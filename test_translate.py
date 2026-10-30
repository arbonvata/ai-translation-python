import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import translate as t


class TranslationTests(unittest.TestCase):
    def test_extract_and_write_back_markup_whitespace_and_namespaces(self):
        xml = '''<resources xmlns:tools="http://schemas.android.com/tools"
                  xmlns:xliff="urn:oasis:names:tc:xliff:document:1.2">
    <string name="plain" tools:ignore="MissingTranslation">  Fish &amp; chips  </string>
    <string name="rich" tools:ignore="MissingTranslation"> Hello <b>world</b> <xliff:g id="name">%1$s</xliff:g>! </string>
    <string-array name="days" tools:ignore="MissingTranslation"><item> A <i>day</i> </item></string-array>
    <plurals name="count" tools:ignore="MissingTranslation"><item quantity="other"> <b>%d</b> items </item></plurals>
</resources>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'values' / 'strings.xml'
            source.parent.mkdir()
            source.write_text(xml)
            resources = t.extract_missing_translations(source)
            self.assertEqual(resources[0].value, '  Fish & chips  ')
            self.assertNotIn('markup', resources[0].to_dict())
            self.assertTrue(resources[1].markup)
            self.assertIn('<b>world</b>', resources[1].value)
            self.assertIn('xmlns:xliff=', resources[1].value)
            entries = [r.to_dict() for r in resources]
            with contextlib.redirect_stdout(io.StringIO()):
                t.write_language_file(str(source), 'da', entries)
                dest = Path(tmp) / 'values-da' / 'strings.xml'
                first = dest.read_text()
                t.write_language_file(str(source), 'da', entries)
            self.assertEqual(dest.read_text(), first)
            root = ET.fromstring(first)
            self.assertEqual(root.find("string[@name='plain']").text, '  Fish & chips  ')
            rich = root.find("string[@name='rich']")
            self.assertEqual(rich.text, ' Hello ')
            self.assertEqual(rich.find('b').tail, ' ')
            self.assertEqual(rich.find('{urn:oasis:names:tc:xliff:document:1.2}g').text, '%1$s')
            self.assertIsNotNone(root.find('string-array/item/i'))
            self.assertIsNotNone(root.find('plurals/item/b'))
            self.assertNotIn('MissingTranslation', first)

    def test_whitespace_only_values_and_inline_gaps_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'values' / 'strings.xml'
            source.parent.mkdir()
            source.write_text('<resources xmlns:tools="http://schemas.android.com/tools">'
                              '<string name="spaces" tools:ignore="MissingTranslation">   </string>'
                              '<string name="gap" tools:ignore="MissingTranslation"><b> </b>  <i>x</i></string>'
                              '</resources>')
            resources = t.extract_missing_translations(source)
            self.assertEqual(resources[0].value, '   ')
            self.assertIn('<b> </b>  <i>x</i>', resources[1].value)
            with contextlib.redirect_stdout(io.StringIO()):
                t.write_language_file(str(source), 'da', [r.to_dict() for r in resources])
                dest = Path(tmp) / 'values-da' / 'strings.xml'
                first = dest.read_text()
                t.write_language_file(str(source), 'da', [r.to_dict() for r in resources])
            self.assertEqual(first, dest.read_text())
            self.assertEqual(ET.fromstring(first).find('string').text, '   ')

    def test_write_back_escapes_text_not_xml_attributes(self):
        soup = t.BeautifulSoup('<resources/>', 'xml')
        entry = {'name': 'rich', 'value': '<b title="it\'s">It\'s &amp; yours</b>', 'markup': True}
        t.upsert_string(soup, soup.resources, entry)
        node = ET.fromstring(str(soup)).find('string/b')
        self.assertEqual(node.attrib['title'], "it's")
        self.assertEqual(node.text, "It\\'s & yours")

    def test_malformed_markup_rejected_before_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / 'values' / 'strings.xml')
            with self.assertRaises(ET.ParseError):
                t.write_language_file(source, 'da', [{'name': 'bad', 'value': '<b>broken', 'markup': True}])
            self.assertFalse((Path(tmp) / 'values-da').exists())

    def test_repeated_placeholder_loss_is_reported(self):
        self.assertEqual(t.placeholder_warnings('%1$s and %1$s', '%1$s'), ['%1$s'])
        self.assertEqual(t.placeholder_warnings('%1$s %d', '%d %1$s'), [])

    def test_proxy_batching_override_and_response_order(self):
        resources = [t.StringResource(str(i), 'Hello', 'No context found') for i in range(51)]
        for override in (False, True):
            with self.subTest(override=override):
                def reply(*args, **kwargs):
                    payload = kwargs['json']
                    self.assertEqual(payload.get('override', False), override)
                    self.assertEqual('override' in payload, override)
                    self.assertNotIn('tag_handling', payload)
                    return Mock(json=lambda: {'translations': [
                        {'id': r['id'], 'text': r['id']} for r in reversed(payload['texts'])]})
                with patch.object(t.requests, 'post', side_effect=reply) as post:
                    result = t.proxy_translate_resources(resources, 'da', 'http://localhost:8080/', 'local', override=override)
                self.assertEqual(post.call_count, 2)
                self.assertEqual([r.value for r in result], [r.name for r in resources])

    def test_xml_transport_for_proxy_and_direct_api(self):
        resources = [t.StringResource('rich', '<b>Hello</b>', 'No context found', markup=True),
                     t.StringResource('plain', 'Fish & chips < 3', 'No context found')]
        def reply(*args, **kwargs):
            payload = kwargs['json']
            self.assertEqual(payload['tag_handling'], 'xml')
            self.assertEqual(payload['ignore_tags'], ['xliff:g'])
            if 'texts' in payload:
                values = [{'id': r['id'], 'text': r['text']} for r in payload['texts']]
            else:
                values = [{'text': value} for value in payload['text']]
            return Mock(json=lambda: {'translations': values})
        with patch.object(t.requests, 'post', side_effect=reply):
            proxy = t.proxy_translate_resources(resources, 'da', 'http://localhost', 'local')
            direct = t.translate_resources(resources, 'da', 'local')
        for result in (proxy, direct):
            self.assertEqual(result, resources)

    def test_proxy_rejects_missing_and_duplicate_ids(self):
        resources = [t.StringResource('name', 'Hello', '')]
        for values in ([], [{'id': 'name', 'text': 'Hej'}] * 2):
            with patch.object(t.requests, 'post', return_value=Mock(json=lambda: {'translations': values})):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    t.proxy_translate_resources(resources, 'da', 'http://localhost', 'local')

    def test_direct_api_batches_and_rejects_incomplete_response(self):
        def reply(*args, **kwargs):
            self.assertNotIn('override', kwargs['json'])
            return Mock(json=lambda: {'translations': [
                {'text': value} for value in kwargs['json']['text']]})
        with patch.object(t.requests, 'post', side_effect=reply) as post:
            values = [str(i) for i in range(51)]
            self.assertEqual(t.deepl_translate_texts(values, 'da', 'key', ids=values), values)
            self.assertEqual(post.call_count, 2)
        with patch.object(t.requests, 'post', return_value=Mock(json=lambda: {'translations': []})):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                t.deepl_translate_texts(['Hello'], 'da', 'key', ids=['name'])

    def test_full_pipeline_propagates_override_and_removes_completed_markers(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'values' / 'strings.xml'
            source.parent.mkdir()
            source.write_text('<resources xmlns:tools="http://schemas.android.com/tools">'
                              '<string name="hello" tools:ignore="MissingTranslation">Hello <b>world</b></string>'
                              '</resources>')
            resources = t.extract_missing_translations(source)
            report = Path(tmp) / 'input.json'
            output = Path(tmp) / 'output.json'
            report.write_text(json.dumps([{'file': str(source), 'missing_translations': [r.to_dict() for r in resources]}]))
            def reply(*args, **kwargs):
                self.assertTrue(kwargs['json']['override'])
                return Mock(json=lambda: {'translations': [
                    {'id': item['id'], 'text': item['text']} for item in kwargs['json']['texts']]})
            with patch.dict(os.environ, {'DEEPL_BASE_URL': 'http://localhost', 'DEEPL_API_KEY': 'local'}):
                with patch.object(t.requests, 'post', side_effect=reply) as post:
                    with contextlib.redirect_stdout(io.StringIO()):
                        t.run_translation(str(report), str(output), override=True)
                        t.run_write_back(str(output))
                self.assertEqual(post.call_count, len(t.TARGET_LANGUAGES))
            self.assertNotIn('MissingTranslation', source.read_text())
            for language in t.TARGET_LANGUAGES:
                self.assertTrue(output.with_name(f'output_{language}.json').exists())
                self.assertIsNotNone(ET.parse(Path(tmp) / f'values-{language}' / 'strings.xml').find('string/b'))

    def test_override_requires_proxy_before_reading_input(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SystemExit, 'requires DEEPL_BASE_URL'):
            t.run_translation('nonexistent', 'nonexistent', override=True)

    def test_cli_help_documents_override(self):
        result = subprocess.run([sys.executable, str(Path(t.__file__)), '--translate', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--override', result.stdout)
        self.assertIn('DEEPL_BASE_URL', result.stdout)


if __name__ == '__main__':
    unittest.main()
