"""Release metadata, archives and announcements shared by the MIMESIS repositories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile

SEMVER = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
MIMESIS_GAME_ID = 9157
API_MIN_VERSION_ID = '39329015529509'
API_FILE_ID = '8129787'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def flag(name):
    value = os.environ.get(name, 'false').lower()
    require(value in ('true', 'false'), f'{name} must be true or false, got {value!r}')
    return value == 'true'


def checked_path(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), f'Path leaves the source checkout: {relative}')
    return path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def outputs(values):
    target = os.environ.get('GITHUB_OUTPUT')
    if not target:
        return
    with Path(target).open('a', encoding='utf-8') as out:
        for name, value in values.items():
            delimiter = 'MIMESIS_' + uuid.uuid4().hex
            out.write(f'{name}<<{delimiter}\n{value}\n{delimiter}\n')


def section(text, version):
    matches = list(re.finditer(r'^## \[' + re.escape(version) + r'\](?:[^\n]*)$', text, re.M))
    require(len(matches) == 1, f'CHANGELOG.md needs exactly one [{version}] section; found {len(matches)}')
    body = re.split(r'^## \[', text[matches[0].end():], maxsplit=1, flags=re.M)[0].strip()
    require(bool(body), f'CHANGELOG.md section [{version}] is empty')
    require(not re.search('[\u2013\u2014]', body), f'CHANGELOG.md [{version}] contains a non-plain dash')
    return body


def inline(text, bbcode):
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'[b]\1[/b]' if bbcode else r'\1', text)
    return re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'[url=\2]\1[/url]' if bbcode else r'\1 (\2)', text)


def render_notes(markdown, bbcode):
    lines, pending, level = [], '', 0

    def flush():
        nonlocal pending
        if pending:
            lines.append(inline(pending, bbcode))
            pending = ''

    def lists(wanted):
        nonlocal level
        flush()
        while level > wanted:
            if bbcode:
                lines.append('[/list]')
            level -= 1
        while level < wanted:
            if bbcode:
                lines.append('[list]')
            level += 1

    for raw in markdown.splitlines():
        if not raw.strip():
            flush()
            continue
        heading = re.match(r'^#+\s+(.+)$', raw)
        bullet = re.match(r'^(\s*)[-*]\s+(.+)$', raw)
        if heading:
            lists(0)
            label = inline(heading[1], bbcode)
            lines.append(f'[b]{label}[/b]' if bbcode else label)
        elif bullet:
            lists(2 if len(bullet[1]) >= 2 else 1)
            pending = ('[*]' if bbcode else '  ' if level == 2 else '') + bullet[2]
        elif pending:
            pending += ' ' + raw.strip()
        else:
            lists(0)
            pending = raw.strip()
    lists(0)
    return '\n'.join(lines)


def nexus_url(uid):
    require(str(uid).isdigit(), f'NEXUS_MOD_ID must be a numeric global mod UID, got {uid!r}')
    game, page = divmod(int(uid), 2 ** 32)
    require(game == MIMESIS_GAME_ID and page > 0,
            f'NEXUS_MOD_ID targets game {game}, page {page}; expected MIMESIS game {MIMESIS_GAME_ID}')
    return f'https://www.nexusmods.com/mimesis/mods/{page}'


def metadata(root, package, tag_release):
    project_name = os.environ['CI_PROJECT'].replace('\\', '/')
    project = checked_path(root, project_name)
    require(project.is_file() and project.suffix == '.csproj', f'Project not found: {project_name}')
    xml = ET.parse(project).getroot()
    assembly = os.environ['CI_ASSEMBLY']
    require(bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', assembly)), f'Invalid assembly name: {assembly!r}')
    require((xml.findtext('.//AssemblyName') or project.stem) == assembly, 'Assembly name differs from CI_ASSEMBLY')
    framework = xml.findtext('.//TargetFramework')
    require(framework == 'netstandard2.1', f'Unexpected MIMESIS target framework: {framework}')
    library = flag('CI_LIBRARY')
    declared = xml.findtext('.//Version')
    manifest = json.loads((root / 'thunderstore/manifest.json').read_text(encoding='utf-8-sig'))
    require(manifest.get('name') == assembly, 'Thunderstore package name differs from CI_ASSEMBLY')
    require(manifest.get('version_number') == '0.0.0', 'Manifest version must remain the 0.0.0 release placeholder')
    if not library:
        require(declared == '0.0.0-dev', f'Mod project version must remain 0.0.0-dev, got {declared!r}')
        core = root / 'Core.cs'
        require(core.is_file() and 'ModVersion.Current' in core.read_text(encoding='utf-8-sig'),
                'Core.cs must read ModVersion.Current')
    readmes = [root / 'README.md', root / 'thunderstore/README.md']
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    require(bool(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)), 'GITHUB_REPOSITORY is missing or invalid')
    short = repository.split('/')[1]
    slug = re.sub(r'^Mimesis-', '', short).lower()
    support = f'> 🛟 **Need help or found a bug?** Get support at [support.doodesch.de/{slug}](https://support.doodesch.de/{slug}).'
    for readme in readmes:
        text = readme.read_text(encoding='utf-8')
        require(not text.startswith('\ufeff'), f'{readme.name} must use UTF-8 without BOM')
        require(text.count(support) == 1, f'{readme}: expected one support line for {slug}')
    version = os.environ.get('CI_VERSION', '').strip()
    if tag_release:
        ref = os.environ.get('GITHUB_REF', '')
        require(os.environ.get('GITHUB_EVENT_NAME') == 'push' and bool(re.fullmatch('refs/tags/v' + SEMVER, ref)),
                f'Releases require a pushed vX.Y.Z tag, got {ref!r}')
        version = ref.removeprefix('refs/tags/v')
        branch = os.environ.get('CI_DEFAULT_BRANCH', 'main')
        subprocess.run(['git', '-C', str(root), 'fetch', '--no-tags', 'origin', branch], check=True)
        result = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', 'HEAD', 'FETCH_HEAD'])
        require(result.returncode == 0, f'Tag {ref} is not on default branch {branch}')
    notes = ''
    if package:
        changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8-sig')
        if not version:
            latest = re.search(r'^## \[(' + SEMVER + r')\]', changelog, re.M)
            require(latest is not None, 'CHANGELOG.md has no stable version section')
            version = latest[1]
        require(bool(re.fullmatch(SEMVER, version)), f'Package version must be X.Y.Z, got {version!r}')
        notes = section(changelog, version)
        if library:
            require(declared == version, f'Library project version {declared!r} does not match {version}')
    else:
        version = declared or '0.0.0-dev'
    license_name = next((name for name in ['LICENSE.md', 'LICENSE'] if (root / name).is_file()), None)
    require(license_name is not None, 'Repository license is missing')
    publish_nexus = flag('CI_NEXUS')
    require(not flag('CI_API_REQUIREMENT') or publish_nexus, 'A Nexus API requirement needs Nexus publishing enabled')
    uid, file_id = os.environ.get('NEXUS_MOD_ID', ''), os.environ.get('NEXUS_FILE_GROUP_ID', '')
    link = ''
    if publish_nexus:
        link = nexus_url(uid)
        require(file_id.isdigit() and int(file_id) > 0, 'NEXUS_FILE_GROUP_ID must identify the existing Nexus file')
    return {
        'name': assembly, 'project': project_name, 'project_directory': project.parent.relative_to(root).as_posix(),
        'framework': framework, 'library': library, 'api_dependency': flag('CI_API_DEPENDENCY'),
        'description': manifest['description'],
        'version': version, 'notes': notes, 'notes_bbcode': render_notes(notes, True),
        'notes_plain': render_notes(notes, False), 'license': license_name, 'repository': repository,
        'thunderstore': flag('CI_THUNDERSTORE'), 'nexus': publish_nexus, 'nexus_url': link,
        'nexus_mod_id': uid, 'nexus_file_id': file_id,
        'api_requirement': flag('CI_API_REQUIREMENT'),
    }


def package_archives(root, meta, destination):
    destination.mkdir(parents=True, exist_ok=True)
    name, version = meta['name'], meta['version']
    binary = root / meta['project_directory'] / 'bin/Release' / meta['framework'] / (name + '.dll')
    dll = binary.read_bytes()
    require(dll.startswith(b'MZ'), f'{binary} is not a PE assembly')
    require(version.encode() in dll or version.encode('utf-16-le') in dll, f'{binary} does not carry version {version}')
    manifest = json.loads((root / 'thunderstore/manifest.json').read_text(encoding='utf-8-sig'))
    manifest['version_number'] = version
    common = {name + '.dll': dll, meta['license']: (root / meta['license']).read_bytes(),
              'CHANGELOG.md': (root / 'CHANGELOG.md').read_bytes()}
    flat = common | {'README.md': (root / 'thunderstore/README.md').read_bytes(),
                     'icon.png': (root / 'thunderstore/icon.png').read_bytes(),
                     'manifest.json': (json.dumps(manifest, indent=2) + '\n').encode()}
    nexus = {('Mods/' + filename if filename.endswith('.dll') else filename): content for filename, content in common.items()}
    nexus['README.md'] = (root / 'README.md').read_bytes()
    receipts = {}
    for suffix, files in [('', flat), ('_nexus', nexus)]:
        path = destination / f'{name}_{version}{suffix}.zip'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for filename, content in sorted(files.items()):
                entry = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, content)
        with zipfile.ZipFile(path) as archive:
            require(archive.testzip() is None, f'Archive CRC failure: {path.name}')
            dll_names = [x for x in archive.namelist() if x.endswith('.dll')]
            require(len(dll_names) == 1 and archive.read(dll_names[0]) == dll, f'Archive payload mismatch: {path.name}')
        receipts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    meta.update(dll_sha256=hashlib.sha256(dll).hexdigest(), archives=receipts,
                description=manifest['description'], dependencies=manifest.get('dependencies', []))
    write_json(destination / 'release.json', meta)
    print(f'info: verified {name} {version}: one fresh DLL in each archive, SHA256 {meta["dll_sha256"]}')


def existing_release(meta, destination, metadata_path):
    version = os.environ.get('CI_VERSION', '')
    require(bool(re.fullmatch(SEMVER, version)), 'An existing release needs an explicit X.Y.Z version')
    command = ['gh', 'api', f'repos/{meta["repository"]}/releases/tags/v{version}']
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    require(result.returncode == 0, f'GitHub release lookup failed: {result.stderr or result.stdout}')
    release = json.loads(result.stdout)
    filename = f'{meta["name"]}_{version}_nexus.zip'
    matches = [asset for asset in release['assets'] if asset['name'] == filename]
    require(len(matches) == 1, f'Published release v{version} needs exactly one {filename} asset')
    asset = matches[0]
    require(bool(re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest', ''))), f'{filename} has no GitHub SHA256 digest')
    with urllib.request.urlopen(asset['browser_download_url'], timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    require(asset['digest'] == 'sha256:' + digest, f'GitHub digest mismatch: {filename}')
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / filename
    path.write_bytes(data)
    with zipfile.ZipFile(path) as archive:
        require(archive.testzip() is None, f'Published ZIP CRC failure: {filename}')
        expected_dll = 'Mods/' + meta['name'] + '.dll'
        dll_names = [name for name in archive.namelist() if name.endswith('.dll')]
        require(dll_names == [expected_dll], f'Published Nexus archive has an unexpected DLL layout: {dll_names}')
        dll = archive.read(expected_dll)
        require(version.encode() in dll or version.encode('utf-16-le') in dll, 'Published DLL does not carry the requested version')
        notes = section(archive.read('CHANGELOG.md').decode('utf-8-sig'), version)
    meta.update(version=version, notes=notes, notes_bbcode=render_notes(notes, True),
                notes_plain=render_notes(notes, False), dll_sha256=hashlib.sha256(dll).hexdigest(),
                archives={filename: digest}, published_asset=asset['browser_download_url'])
    write_json(metadata_path, meta)
    outputs({k: str(v).lower() if isinstance(v, bool) else v for k, v in meta.items() if isinstance(v, (str, bool))})
    print(f'info: confirmed published {filename}, SHA256 {digest}; no source rebuild performed')


def discord_payload(meta, published):
    require(published.get('github') == 'success', 'Cannot announce a failed GitHub release')
    for store in ('thunderstore', 'nexus'):
        require(not meta[store] or published.get(store) == 'success', f'Cannot announce {store}: {published.get(store)}')
    require(not meta['api_requirement'] or published.get('requirements') == 'success', 'Cannot announce failed Nexus requirements')
    repo, version, name = meta['repository'], meta['version'], meta['name']
    url = f'https://github.com/{repo}/releases/tag/v{version}'
    links = []
    if meta['nexus']:
        links.append(f'[Nexus]({meta["nexus_url"]})')
    if meta['thunderstore']:
        links.append(f'[Thunderstore](https://thunderstore.io/c/mimesis/p/DooDesch/{name}/)')
    links.append(f'[GitHub]({url})')
    notes = meta['notes']
    if len(notes) > 3800:
        notes = notes[:3700].rsplit(' ', 1)[0] + f'\n\n[Full release notes]({url})'
    return {'embeds': [{'title': f'{name} {version}', 'url': url, 'description': notes, 'color': 8256434,
                        'thumbnail': {'url': f'https://raw.githubusercontent.com/{repo}/main/thunderstore/icon.png'},
                        'fields': [{'name': 'Get it', 'value': ' · '.join(links)}],
                        'footer': {'text': f'Mimesis · {repo.split("/")[1]}'}}]}


def nexus_request(method, endpoint, body=None):
    key = os.environ.get('NEXUSMODS_API_KEY', '')
    require(bool(key), 'NEXUSMODS_API_KEY is missing')
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request('https://api.nexusmods.com/v3/' + endpoint, data=data, method=method,
                                     headers={'apikey': key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        message = error.read(2048).decode('utf-8', errors='replace').replace(key, '[redacted]')
        raise RuntimeError(f'Nexus {method} {endpoint}: HTTP {error.code}: {message}') from error


def normalize_ranges(value):
    return {'dependency_definitions': [
        {'ranges': [{'min_version_id': str(r['min_version']['id']),
                     'max_version_id': str(r['max_version']['id']) if r.get('max_version') else None}
                    for r in definition['ranges']]}
        for definition in value['dependency_definitions']]}


def set_requirements(meta, version_id, receipt_dir):
    require(str(version_id).isdigit(), f'Invalid uploaded Nexus version ID: {version_id!r}')
    source = nexus_request('GET', f'mod-file-versions/{version_id}')
    require(source['data']['file']['id'] == meta['nexus_file_id'], 'Uploaded Nexus file differs from the configured file')
    require(source['data']['version'] == meta['version'], 'Uploaded Nexus version differs from the requested release')
    minimum = nexus_request('GET', f'mod-file-versions/{API_MIN_VERSION_ID}')
    require(minimum['data']['file']['id'] == API_FILE_ID, 'MimicAPI requirement points at a different file')
    endpoint = f'mod-file-versions/{version_id}/dependencies/ranges'
    before = nexus_request('GET', endpoint)
    wanted = normalize_ranges(before)
    found = any(r['min_version_id'] == API_MIN_VERSION_ID and r['max_version_id'] is None
                for definition in wanted['dependency_definitions'] for r in definition['ranges'])
    if not found:
        wanted['dependency_definitions'].append({'ranges': [{'min_version_id': API_MIN_VERSION_ID, 'max_version_id': None}]})
        nexus_request('PUT', endpoint, wanted)
    after = nexus_request('GET', endpoint)
    require(normalize_ranges(after) == wanted, f'Nexus requirement readback differs for version {version_id}')
    write_json(receipt_dir / 'nexus-requirements.json', {'source': source, 'before': before, 'wanted': wanted, 'after': after})
    print(f'info: Nexus version {version_id} requires the maintained MimicAPI; existing ranges retained and read back')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['metadata', 'package', 'existing', 'discord', 'requirements'])
    parser.add_argument('--root', type=Path, default=Path('source'))
    parser.add_argument('--metadata', type=Path, default=Path('release-meta/release.json'))
    parser.add_argument('--out', type=Path, default=Path('dist'))
    parser.add_argument('--package', action='store_true')
    parser.add_argument('--tag-release', action='store_true')
    args = parser.parse_args()
    if args.command == 'metadata':
        value = metadata(args.root.resolve(), args.package, args.tag_release)
        write_json(args.metadata, value)
        outputs({k: str(v).lower() if isinstance(v, bool) else v for k, v in value.items()})
        print(f'info: metadata validated: {value["name"]} {value["version"]}, package={args.package}, tag_release={args.tag_release}')
    else:
        value = json.loads(args.metadata.read_text(encoding='utf-8'))
        if args.command == 'package':
            package_archives(args.root.resolve(), value, args.out)
        elif args.command == 'existing':
            existing_release(value, args.out, args.metadata)
        elif args.command == 'discord':
            payload = discord_payload(value, json.loads(os.environ['CI_STORE_RESULTS']))
            write_json(args.out / 'discord.json', payload)
        else:
            set_requirements(value, os.environ.get('NEXUS_VERSION_ID'), args.out)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'error: MIMESIS CI {sys.argv[1] if len(sys.argv) > 1 else "startup"}: {type(error).__name__}: {error}', file=sys.stderr)
        raise
