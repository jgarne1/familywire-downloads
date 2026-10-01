"""Publish only the eight verified FamilyWire release assets, atomically from draft."""
import argparse, base64, hashlib, json, os, pathlib, re, sys, urllib.error, urllib.parse, urllib.request, zipfile

VERSION = '1.2.0'
FILES = {'FamilyWire-Setup-x64.exe', 'FamilyWire-Setup-x64.exe.blockmap',
         'FamilyWire-macOS-x64.zip', 'FamilyWire-macOS-x64.zip.blockmap',
         'FamilyWire-macOS-arm64.zip', 'FamilyWire-macOS-arm64.zip.blockmap',
         'latest.yml', 'latest-mac.yml'}
ROOT = pathlib.Path(__file__).resolve().parent

def digest(path, algorithm='sha256'):
    hasher = hashlib.new(algorithm)
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            hasher.update(block)
    return hasher.digest()

def collect(inputs, archives, assets):
    assets.mkdir(parents=True, exist_ok=True)
    archives.mkdir(parents=True, exist_ok=True)
    found = set()
    for item in inputs['artifacts']:
        if item['name'] not in {'FamilyWire-macOS', 'FamilyWire-Windows'}:
            raise ValueError('Unexpected artifact name')
        archive = archives / (item['name']+'.zip')
        if not archive.exists():
            url = urllib.parse.urlparse(item['url'])
            if url.scheme != 'https' or not url.hostname.endswith('.oaiusercontent.com'):
                raise ValueError('Unexpected release transfer destination')
            with urllib.request.urlopen(item['url'], timeout=180) as response, archive.open('wb') as target:
                while block := response.read(1024*1024):
                    target.write(block)
        if digest(archive).hex() != item['sha256']:
            raise ValueError('CI artifact digest mismatch')
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                name = pathlib.PurePosixPath(entry.filename).name
                if name not in FILES:
                    continue
                if entry.filename != 'release-out/'+name or name in found:
                    raise ValueError('Unexpected or duplicate release asset path')
                with bundle.open(entry) as source, (assets/name).open('wb') as target:
                    while block := source.read(1024*1024):
                        target.write(block)
                found.add(name)
    if found != FILES:
        raise ValueError('Release must contain exactly eight expected assets')
    for manifest, expected in [('latest.yml', {'FamilyWire-Setup-x64.exe'}),
                               ('latest-mac.yml', {'FamilyWire-macOS-x64.zip', 'FamilyWire-macOS-arm64.zip'})]:
        text = (assets/manifest).read_text()
        if not re.search(r'^version: [\'"]?1\.2\.0[\'"]?\s*$', text, re.M):
            raise ValueError('Update manifest version mismatch')
        records = []
        for line in text.splitlines():
            if re.match(r'^\s+- url:', line):
                records.append({'url': line.split(':', 1)[1].strip().strip('\'"')})
            elif records and re.match(r'^\s+(sha512|size):', line):
                key, value = line.strip().split(':', 1)
                records[-1][key] = value.strip().strip('\'"')
        if {r['url'] for r in records} != expected or len(records) != len(expected):
            raise ValueError('Update manifest asset list mismatch')
        for record in records:
            package = assets/record['url']
            if int(record['size']) != package.stat().st_size:
                raise ValueError('Update manifest size mismatch')
            if record['sha512'] != base64.b64encode(digest(package, 'sha512')).decode():
                raise ValueError('Update manifest SHA512 mismatch')
        defaults = dict(re.findall(r'^(path|sha512): (.+)$', text, re.M))
        if defaults.get('path') not in expected or defaults.get('sha512') != base64.b64encode(digest(assets/defaults['path'], 'sha512')).decode():
            raise ValueError('Default update manifest package mismatch')
    return [{'name': n, 'size': (assets/n).stat().st_size, 'sha256': digest(assets/n).hex()} for n in sorted(FILES)]

def publish(inputs, assets, verified):
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'jgarne1/familywire-downloads':
        raise ValueError('Publishing is restricted to the FamilyWire download repository')
    token = os.environ['GH_TOKEN']
    api = 'https://api.github.com/repos/'+repository
    def request(url, method='GET', payload=None, binary=None):
        host = urllib.parse.urlparse(url).hostname
        if host not in {'api.github.com', 'uploads.github.com'}:
            raise ValueError('Unexpected publication API destination')
        headers = {'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'FamilyWire-release-publisher'}
        data = json.dumps(payload).encode() if payload is not None else None
        if binary:
            data = binary.read_bytes()
            headers['Content-Type'] = 'application/octet-stream'
        else:
            headers['Content-Type'] = 'application/json'
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=300) as response:
            return json.load(response)
    try:
        release = request(api+'/releases/tags/v'+VERSION)
    except urllib.error.HTTPError as problem:
        if problem.code != 404:
            raise
        release = request(api+'/releases', 'POST', {'tag_name': 'v'+VERSION, 'target_commitish': os.environ['GITHUB_SHA'],
                          'name': 'FamilyWire 1.2.0: Fleet Duel', 'draft': True, 'prerelease': False,
                          'body': inputs['release_notes']})
    existing = {a['name']: a for a in release['assets']}
    if not release['draft'] and set(existing) != FILES:
        raise ValueError('Existing published release differs; refusing to modify it')
    for item in verified:
        if item['name'] in existing:
            asset = existing[item['name']]
            if asset['size'] != item['size'] or asset.get('digest') != 'sha256:'+item['sha256']:
                raise ValueError('Existing release asset differs; refusing replacement')
            continue
        if not release['draft']:
            raise ValueError('Refusing to append to a published release')
        url = release['upload_url'].split('{')[0]+'?name='+urllib.parse.quote(item['name'])
        result = request(url, 'POST', binary=assets/item['name'])
        if result['size'] != item['size'] or result.get('digest') != 'sha256:'+item['sha256']:
            raise ValueError('Uploaded release asset digest mismatch')
        print('Verified uploaded asset:', item['name'])
    check = request(api+'/releases/'+str(release['id']))
    if {a['name'] for a in check['assets']} != FILES:
        raise ValueError('Draft release is incomplete')
    if check['draft']:
        check = request(api+'/releases/'+str(release['id']), 'PATCH', {'draft': False, 'prerelease': False, 'make_latest': 'true'})
    if check['draft'] or check['prerelease']:
        raise ValueError('Release publication failed')
    print('Published:', check['html_url'])

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--archives', default='release-archives')
    parser.add_argument('--assets', default='release-assets')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    inputs = json.loads(pathlib.Path(args.inputs).read_text())
    if inputs['version'] != VERSION or inputs['source_tree'] != 'e95294674bc23c5280d2e3cb2a7fab07b48ed29e':
        raise ValueError('Unexpected release source')
    verified = collect(inputs, pathlib.Path(args.archives), pathlib.Path(args.assets))
    pathlib.Path(args.assets, 'verified-assets.json').write_text(json.dumps(verified, indent=2))
    print('Validated eight release assets and both update manifests for', VERSION)
    if not args.verify_only:
        publish(inputs, pathlib.Path(args.assets), verified)

