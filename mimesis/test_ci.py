import contextlib
import copy
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

SPEC = importlib.util.spec_from_file_location('mimesis_ci', Path(__file__).with_name('ci.py'))
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class ReleaseContract(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(__file__).resolve().parent / '.test-tmp'
        self.temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='mimesis-', dir=self.temp_root)
        self.addCleanup(self.cleanup_temp)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        (self.root / 'thunderstore').mkdir()
        (self.root / 'Demo.csproj').write_text('<Project><PropertyGroup><TargetFramework>netstandard2.1</TargetFramework><Version>0.0.0-dev</Version><AssemblyName>Demo</AssemblyName></PropertyGroup></Project>')
        (self.root / 'Core.cs').write_text('const string Version = ModVersion.Current;')
        (self.root / 'LICENSE.md').write_text('Original human copyright and license')
        (self.root / 'CHANGELOG.md').write_text('# Changelog\n\n## [1.2.3]\n\n### Fixed\n\n- A "quoted" fix with `settings` and [details](https://example.com).\n  Continued text.\n\n## [1.2.2]\n\n- Older change.\n', encoding='utf-8')
        support = '> 🛟 **Need help or found a bug?** Get support at [support.doodesch.de/demo](https://support.doodesch.de/demo).'
        for name in ('README.md', 'thunderstore/README.md'):
            (self.root / name).write_text('# Demo\n\n' + support + '\n', encoding='utf-8')
        (self.root / 'thunderstore/manifest.json').write_text(json.dumps({'name': 'Demo', 'version_number': '0.0.0', 'description': 'A demo', 'dependencies': ['LavaGang-MelonLoader-0.7.3']}))
        (self.root / 'thunderstore/icon.png').write_bytes(b'icon fixture')
        self.env = mock.patch.dict(os.environ, {
            'CI_PROJECT': 'Demo.csproj', 'CI_ASSEMBLY': 'Demo', 'CI_LIBRARY': 'false',
            'CI_API_DEPENDENCY': 'false', 'CI_API_REQUIREMENT': 'false', 'CI_THUNDERSTORE': 'true',
            'CI_NEXUS': 'true', 'CI_VERSION': '1.2.3', 'GITHUB_REPOSITORY': 'DooDesch-Mods/Mimesis-Demo',
            'NEXUS_MOD_ID': str(ci.MIMESIS_GAME_ID * 2 ** 32 + 7), 'NEXUS_FILE_GROUP_ID': '12345',
            'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/tags/v1.2.3',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def cleanup_temp(self):
        path = Path(self.temp.name)
        ci.require(path.resolve().parent == self.temp_root.resolve() and not path.is_symlink(),
                   f'Test cleanup target leaves its own temporary directory: {path}')
        self.temp.cleanup()

    def meta(self):
        return ci.metadata(self.root, True, False)

    def test_missing_and_duplicate_changelog_fail(self):
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            ci.section('## [1.0.0]\n- Other', '1.2.3')
        with self.assertRaisesRegex(ValueError, 'found 2'):
            ci.section('## [1.2.3]\n- A\n## [1.2.3]\n- B', '1.2.3')
        with self.assertRaisesRegex(ValueError, 'empty'):
            ci.section('## [1.2.3]\n\n## [1.2.2]\n- Other', '1.2.3')

    def test_rendering_preserves_sections_links_and_continuations(self):
        meta = self.meta()
        self.assertEqual(meta['notes_plain'], 'Fixed\nA "quoted" fix with settings and details (https://example.com). Continued text.')
        self.assertIn('[b]Fixed[/b]\n[list]\n[*]A "quoted" fix', meta['notes_bbcode'])
        self.assertIn('[url=https://example.com]details[/url]', meta['notes_bbcode'])
        self.assertTrue(meta['notes_bbcode'].endswith('[/list]'))

    def test_nested_lists_and_paragraphs(self):
        text = '### Added\n\nA paragraph\ncontinued.\n\n- Parent\n  - Child\n- Next'
        bbcode = ci.render_notes(text, True)
        self.assertEqual(bbcode.count('[list]'), bbcode.count('[/list]'))
        self.assertIn('A paragraph continued.', bbcode)
        self.assertIn('  Child', ci.render_notes(text, False))

    def test_tag_shape_and_branch_are_checked_before_release(self):
        os.environ['GITHUB_REF'] = 'refs/tags/vNext'
        with self.assertRaisesRegex(ValueError, 'pushed vX.Y.Z'):
            ci.metadata(self.root, True, True)
        os.environ['GITHUB_REF'] = 'refs/tags/v1.2.3'
        with mock.patch.object(ci.subprocess, 'run', side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=1)]):
            with self.assertRaisesRegex(ValueError, 'not on default branch'):
                ci.metadata(self.root, True, True)

    def test_project_placeholder_and_target_game_are_enforced(self):
        project = self.root / 'Demo.csproj'
        project.write_text(project.read_text().replace('0.0.0-dev', '1.2.3'))
        with self.assertRaisesRegex(ValueError, '0.0.0-dev'):
            self.meta()
        with self.assertRaisesRegex(ValueError, 'expected MIMESIS'):
            ci.nexus_url(7381 * 2 ** 32 + 7)

    def test_project_cannot_leave_the_checkout(self):
        os.environ['CI_PROJECT'] = '../outside.csproj'
        with self.assertRaisesRegex(ValueError, 'leaves the source'):
            self.meta()

    def test_package_ignores_stale_dll_and_keeps_nexus_layout(self):
        meta = self.meta()
        dll = b'MZ fresh assembly fixture 1.2.3'
        binary = self.root / 'bin/Release/netstandard2.1/Demo.dll'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(dll)
        (self.root / 'thunderstore/Demo.dll').write_bytes(b'MZ stale assembly')
        out = Path(self.temp.name) / 'dist'
        ci.package_archives(self.root, meta, out)
        first = copy.deepcopy(meta['archives'])
        ci.package_archives(self.root, meta, out)
        self.assertEqual(first, meta['archives'])
        with zipfile.ZipFile(out / 'Demo_1.2.3.zip') as archive:
            self.assertEqual(archive.read('Demo.dll'), dll)
            self.assertEqual(json.loads(archive.read('manifest.json'))['version_number'], '1.2.3')
        with zipfile.ZipFile(out / 'Demo_1.2.3_nexus.zip') as archive:
            self.assertEqual(archive.read('Mods/Demo.dll'), dll)
            self.assertNotIn('manifest.json', archive.namelist())
            self.assertNotIn('icon.png', archive.namelist())
            self.assertEqual(archive.read('LICENSE.md'), (self.root / 'LICENSE.md').read_bytes())

    def test_discord_links_and_json_survive_quotes(self):
        payload = ci.discord_payload(self.meta(), {'github': 'success', 'nexus': 'success', 'thunderstore': 'success'})
        encoded = json.dumps(payload)
        restored = json.loads(encoded)['embeds'][0]
        self.assertIn('"quoted"', restored['description'])
        links = restored['fields'][0]['value']
        self.assertTrue(links.startswith('[Nexus](https://www.nexusmods.com/mimesis/mods/7)'))
        self.assertIn(' · [Thunderstore]', links)
        self.assertIn(' · [GitHub]', links)

    def published_asset(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('Mods/Demo.dll', b'MZ published DLL 1.2.3')
            archive.writestr('CHANGELOG.md', '## [1.2.3]\n\n- Published notes.\n')
        payload = data.getvalue()
        release = {'assets': [{'name': 'Demo_1.2.3_nexus.zip',
                    'browser_download_url': 'https://example.com/published.zip',
                    'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}]}
        return payload, release

    def test_nexus_repair_preserves_exact_published_bytes_and_notes(self):
        payload, release = self.published_asset()
        out = Path(self.temp.name) / 'repair'
        with mock.patch.object(ci.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(release))), \
                mock.patch.object(ci.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            ci.existing_release(self.meta(), out, out / 'release.json')
        self.assertEqual((out / 'Demo_1.2.3_nexus.zip').read_bytes(), payload)
        self.assertEqual(json.loads((out / 'release.json').read_text())['notes'], '- Published notes.')

    def test_nexus_repair_rejects_digest_mismatch_before_writing(self):
        payload, release = self.published_asset()
        release['assets'][0]['digest'] = 'sha256:' + '0' * 64
        out = Path(self.temp.name) / 'repair'
        with mock.patch.object(ci.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(release))), \
                mock.patch.object(ci.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                ci.existing_release(self.meta(), out, out / 'release.json')
        self.assertFalse(out.exists())

    def test_failed_store_cannot_be_announced(self):
        with self.assertRaisesRegex(ValueError, 'Cannot announce nexus'):
            ci.discord_payload(self.meta(), {'github': 'success', 'nexus': 'failure', 'thunderstore': 'success'})
        meta = self.meta()
        meta['api_requirement'] = True
        with self.assertRaisesRegex(ValueError, 'requirements'):
            ci.discord_payload(meta, {'github': 'success', 'nexus': 'success', 'thunderstore': 'success', 'requirements': 'failure'})

    def test_disabled_store_is_omitted_from_announcement(self):
        meta = self.meta()
        meta['thunderstore'] = False
        payload = ci.discord_payload(meta, {'github': 'success', 'nexus': 'success', 'thunderstore': 'skipped'})
        self.assertNotIn('Thunderstore', payload['embeds'][0]['fields'][0]['value'])

    def test_existing_nexus_requirements_are_retained_and_read_back(self):
        meta = self.meta()
        source = {'data': {'file': {'id': meta['nexus_file_id']}, 'version': meta['version']}}
        minimum = {'data': {'file': {'id': ci.API_FILE_ID}}}
        before = {'dependency_definitions': [{'ranges': [{'min_version': {'id': '111'}, 'max_version': None}]}]}
        after = {'dependency_definitions': before['dependency_definitions'] + [
            {'ranges': [{'min_version': {'id': ci.API_MIN_VERSION_ID}, 'max_version': None}]}]}
        with mock.patch.object(ci, 'nexus_request', side_effect=[source, minimum, before, None, after]) as request:
            ci.set_requirements(meta, '987654', Path(self.temp.name))
        put = request.call_args_list[3]
        self.assertEqual(put.args[0], 'PUT')
        self.assertEqual(put.args[2]['dependency_definitions'][0]['ranges'][0]['min_version_id'], '111')
        receipt = json.loads((Path(self.temp.name) / 'nexus-requirements.json').read_text())
        self.assertEqual(receipt['wanted'], ci.normalize_ranges(receipt['after']))

    def test_nexus_requirement_mismatch_fails(self):
        meta = self.meta()
        with mock.patch.object(ci, 'nexus_request', return_value={'data': {'file': {'id': 'wrong-file'}}}):
            with self.assertRaisesRegex(ValueError, 'differs from the configured file'):
                ci.set_requirements(meta, '987654', Path(self.temp.name))


if __name__ == '__main__':
    unittest.main()
