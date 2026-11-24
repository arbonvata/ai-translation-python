import contextlib
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from translation_tool import android_xml, client, models, workflows
import translate



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
            resources = android_xml.extract_missing_translations(source)
            self.assertEqual(resources[0].value, '  Fish & chips  ')
            self.assertNotIn('markup', resources[0].to_dict())
            self.assertTrue(resources[1].markup)
            self.assertIn('<b>world</b>', resources[1].value)
            self.assertIn('xmlns:xliff=', resources[1].value)
            entries = [r.to_dict() for r in resources]
            with contextlib.redirect_stdout(io.StringIO()):
                android_xml.write_language_file(str(source), 'da', entries)
                dest = Path(tmp) / 'values-da' / 'strings.xml'
                first = dest.read_text()
                android_xml.write_language_file(str(source), 'da', entries)
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
            resources = android_xml.extract_missing_translations(source)
            self.assertEqual(resources[0].value, '   ')
            self.assertIn('<b> </b>  <i>x</i>', resources[1].value)
            with contextlib.redirect_stdout(io.StringIO()):
                android_xml.write_language_file(str(source), 'da', [r.to_dict() for r in resources])
                dest = Path(tmp) / 'values-da' / 'strings.xml'
                first = dest.read_text()
                android_xml.write_language_file(str(source), 'da', [r.to_dict() for r in resources])
            self.assertEqual(first, dest.read_text())
            self.assertEqual(ET.fromstring(first).find('string').text, '   ')

    def test_write_back_escapes_text_not_xml_attributes(self):
        soup = BeautifulSoup('<resources/>', 'xml')
        entry = {'name': 'rich', 'value': '<b title="it\'s">It\'s &amp; yours</b>', 'markup': True}
        android_xml.upsert_string(soup, soup.resources, entry)
        node = ET.fromstring(str(soup)).find('string/b')
        self.assertEqual(node.attrib['title'], "it's")
        self.assertEqual(node.text, "It\\'s & yours")

    def test_malformed_markup_rejected_before_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / 'values' / 'strings.xml')
            with self.assertRaises(ET.ParseError):
                android_xml.write_language_file(source, 'da', [{'name': 'bad', 'value': '<b>broken', 'markup': True}])
            self.assertFalse((Path(tmp) / 'values-da').exists())

    def test_repeated_placeholder_loss_is_reported(self):
        self.assertEqual(models.placeholder_warnings('%1$s and %1$s', '%1$s'), ['%1$s'])
        self.assertEqual(models.placeholder_warnings('%1$s %d', '%d %1$s'), [])

    def test_proxy_batching_override_and_response_order(self):
        resources = [models.StringResource(str(i), 'Hello', 'No context found') for i in range(51)]
        for override in (False, True):
            with self.subTest(override=override):
                def reply(*args, **kwargs):
                    payload = kwargs['json']
                    self.assertEqual(payload.get('override', False), override)
                    self.assertEqual('override' in payload, override)
                    self.assertNotIn('tag_handling', payload)
                    return Mock(json=lambda: {'translations': [
                        {'id': r['id'], 'text': r['id']} for r in reversed(payload['texts'])]})
                with patch.object(client.requests, 'post', side_effect=reply) as post:
                    result = client.proxy_translate_resources(resources, 'da', 'http://localhost:8080/', 'local', override=override)
                self.assertEqual(post.call_count, 2)
                self.assertEqual([r.value for r in result], [r.name for r in resources])

    def test_xml_transport_for_proxy_and_direct_api(self):
        resources = [models.StringResource('rich', '<b>Hello</b>', 'No context found', markup=True),
                     models.StringResource('plain', 'Fish & chips < 3', 'No context found')]
        def reply(*args, **kwargs):
            payload = kwargs['json']
            self.assertEqual(payload['tag_handling'], 'xml')
            self.assertEqual(payload['ignore_tags'], ['xliff:g'])
            if 'texts' in payload:
                values = [{'id': r['id'], 'text': r['text']} for r in payload['texts']]
            else:
                values = [{'text': value} for value in payload['text']]
            return Mock(json=lambda: {'translations': values})
        with patch.object(client.requests, 'post', side_effect=reply):
            proxy = client.proxy_translate_resources(resources, 'da', 'http://localhost', 'local')
            direct = client.translate_resources(resources, 'da', 'local')
        for result in (proxy, direct):
            self.assertEqual(result, resources)

    def test_proxy_rejects_missing_and_duplicate_ids(self):
        resources = [models.StringResource('name', 'Hello', '')]
        for values in ([], [{'id': 'name', 'text': 'Hej'}] * 2):
            with patch.object(client.requests, 'post', return_value=Mock(json=lambda: {'translations': values})):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(models.TranslationError):
                    client.proxy_translate_resources(resources, 'da', 'http://localhost', 'local')

    def test_direct_api_batches_and_rejects_incomplete_response(self):
        def reply(*args, **kwargs):
            self.assertNotIn('override', kwargs['json'])
            return Mock(json=lambda: {'translations': [
                {'text': value} for value in kwargs['json']['text']]})
        with patch.object(client.requests, 'post', side_effect=reply) as post:
            values = [str(i) for i in range(51)]
            self.assertEqual(client.deepl_translate_texts(values, 'da', 'key', ids=values), values)
            self.assertEqual(post.call_count, 2)
        with patch.object(client.requests, 'post', return_value=Mock(json=lambda: {'translations': []})):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(models.TranslationError):
                client.deepl_translate_texts(['Hello'], 'da', 'key', ids=['name'])

    def test_full_pipeline_propagates_override_and_removes_completed_markers(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'values' / 'strings.xml'
            source.parent.mkdir()
            source.write_text('<resources xmlns:tools="http://schemas.android.com/tools">'
                              '<string name="hello" tools:ignore="MissingTranslation">Hello <b>world</b></string>'
                              '</resources>')
            resources = android_xml.extract_missing_translations(source)
            report = Path(tmp) / 'input.json'
            output = Path(tmp) / 'output.json'
            report.write_text(json.dumps([{'file': str(source), 'missing_translations': [r.to_dict() for r in resources]}]))
            def reply(*args, **kwargs):
                self.assertTrue(kwargs['json']['override'])
                return Mock(json=lambda: {'translations': [
                    {'id': item['id'], 'text': item['text']} for item in kwargs['json']['texts']]})
            with patch.dict(os.environ, {'DEEPL_BASE_URL': 'http://localhost', 'DEEPL_API_KEY': 'local'}):
                with patch.object(client.requests, 'post', side_effect=reply) as post:
                    with contextlib.redirect_stdout(io.StringIO()):
                        workflows.run_translation(str(report), str(output), override=True)
                        workflows.run_write_back(str(output))
                self.assertEqual(post.call_count, len(models.TARGET_LANGUAGES))
            self.assertNotIn('MissingTranslation', source.read_text())
            for language in models.TARGET_LANGUAGES:
                self.assertTrue(output.with_name(f'output_{language}.json').exists())
                self.assertIsNotNone(ET.parse(Path(tmp) / f'values-{language}' / 'strings.xml').find('string/b'))

    def test_override_requires_proxy_before_reading_input(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(models.TranslationError, 'requires DEEPL_BASE_URL'):
            workflows.run_translation('nonexistent', 'nonexistent', override=True)

    def test_cli_help_documents_override(self):
        result = subprocess.run([sys.executable, str(Path(translate.__file__)), '--translate', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--override', result.stdout)
        self.assertIn('DEEPL_BASE_URL', result.stdout)


class ClientErrorTests(unittest.TestCase):
    def test_request_failure_is_a_library_error(self):
        with patch.object(client.requests, 'post', side_effect=client.requests.Timeout('too slow')):
            with self.assertRaisesRegex(models.TranslationError, 'request failed'):
                client.deepl_translate_texts(['Hello'], 'da', 'key')

    def test_http_errors_include_service_and_close_response(self):
        response = Mock(text='quota exceeded')
        response.raise_for_status.side_effect = client.requests.HTTPError('456')
        with patch.object(client.requests, 'post', return_value=response):
            with self.assertRaisesRegex(models.TranslationError, 'DeepL API.*quota exceeded'):
                client.deepl_translate_texts(['Hello'], 'da', 'key')
        response.close.assert_called_once()

    def test_invalid_json_and_response_shapes_are_reported(self):
        responses = [Mock(json=Mock(side_effect=ValueError('invalid JSON')))]
        responses += [Mock(json=Mock(return_value=value)) for value in (
            None, [], {}, {'translations': None}, {'translations': [{'text': 3}]},
        )]
        for response in responses:
            with self.subTest(response=response):
                with patch.object(client.requests, 'post', return_value=response):
                    with self.assertRaises(models.TranslationError):
                        client.deepl_translate_texts(['Hello'], 'da', 'key')
                response.close.assert_called_once()

    def test_incomplete_response_without_ids_is_reported(self):
        response = Mock(json=lambda: {'translations': []})
        with patch.object(client.requests, 'post', return_value=response):
            with self.assertRaisesRegex(models.TranslationError, 'unidentified texts'):
                client.deepl_translate_texts(['Hello'], 'da', 'key')

    def test_array_context_and_direct_endpoint_selection(self):
        resources = [models.StringResource('plain', 'Hello', 'Ignored for direct plain strings'),
                     models.StringResource('days', 'Mon', 'Days of the week', type='string-array', index=0)]
        def reply(endpoint, **kwargs):
            self.assertEqual(endpoint, client.DEEPL_FREE_ENDPOINT)
            payload = kwargs['json']
            if payload['text'] == ['Mon']:
                self.assertEqual(payload['context'], 'Days of the week')
            else:
                self.assertNotIn('context', payload)
            return Mock(json=lambda: {'translations': [{'text': text} for text in payload['text']]})
        with patch.object(client.requests, 'post', side_effect=reply) as post:
            self.assertEqual(client.translate_resources(resources, 'da', 'key:fx'), resources)
        self.assertEqual(post.call_count, 2)


class CliTests(unittest.TestCase):
    def test_defaults_and_option_positions(self):
        from translation_tool.cli import parse_arguments
        mode, args = parse_arguments(['--translate'])
        self.assertEqual(mode, 'translate')
        self.assertEqual((args.input, args.output, args.override),
                         ('missing_translations.json', 'translations.json', False))
        for argv in (['--translate', '--override', 'in.json', 'out.json'],
                     ['--translate', 'in.json', 'out.json', '--override']):
            mode, args = parse_arguments(argv)
            self.assertEqual((args.input, args.output, args.override), ('in.json', 'out.json', True))
        mode, args = parse_arguments(['--write-back', '--dry-run'])
        self.assertEqual((mode, args.input, args.dry_run), ('write-back', 'translations.json', True))
        mode, args = parse_arguments(['res'])
        self.assertEqual((mode, args.folder, args.output), ('extract', 'res', 'missing_translations.json'))

    def test_cli_reports_errors_without_tracebacks(self):
        result = subprocess.run(
            [sys.executable, str(Path(translate.__file__)), '--translate', '--override'],
            env={key: value for key, value in os.environ.items() if key not in ('DEEPL_BASE_URL', 'DEEPL_API_KEY')},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires DEEPL_BASE_URL', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_extraction_cli_writes_the_existing_report_shape(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'values' / 'strings.xml'
            source.parent.mkdir()
            source.write_text('<resources xmlns:tools="http://schemas.android.com/tools">'
                              '<string name="hello" tools:ignore="MissingTranslation">Hello</string>'
                              '<string name="skip">Skipped</string></resources>')
            output = Path(tmp) / 'report.json'
            result = subprocess.run(
                [sys.executable, str(Path(translate.__file__)), tmp, str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), [{
                'file': str(source), 'missing_translations': [
                    {'name': 'hello', 'value': 'Hello', 'context': 'No context found'}]}])

    def test_no_arguments_returns_failure_with_usage(self):
        from translation_tool.cli import main
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main([]), 1)
        self.assertIn('--write-back', stderr.getvalue())


class ProjectTranslationTests(unittest.TestCase):
    def test_scans_string_files_and_fills_only_missing_localized_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp)
            values = res / 'values'
            values.mkdir()
            (values / 'strings.xml').write_text('''<resources xmlns:tools="http://schemas.android.com/tools">
                <string name="existing">Existing</string>
                <string name="refresh" tools:ignore="MissingTranslation">Refresh</string>
                <string name="brand" translatable="false">Brand</string>
                <string-array name="days"><item>Monday</item><item>Tuesday</item></string-array>
            </resources>''')
            (values / 'strings_calendar.xml').write_text(
                '<resources><string name="calendar">Calendar</string></resources>')
            (values / 'other.xml').write_text('<resources><string name="ignored">Ignored</string></resources>')
            danish = res / 'values-da'
            danish.mkdir()
            (danish / 'custom.xml').write_text('''<resources>
                <string name="existing">Existing Danish translation</string>
                <string name="refresh">Old Danish translation</string>
                <string-array name="days"><item>Mandag</item></string-array>
            </resources>''')

            def translate_batch(resources, language):
                return [replace(resource, value=f'{language}: {resource.value}') for resource in resources]

            args = ['--project', str(values)]
            with patch.object(workflows, 'configured_client') as configured, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(translate.main([*args, '--dry-run']), 0)
                configured.assert_not_called()
                self.assertFalse((danish / 'strings_calendar.xml').exists())
                configured.return_value.translate.side_effect = translate_batch
                self.assertEqual(translate.main(args), 0)
                configured.reset_mock()
                self.assertEqual(translate.main(args), 0)
                configured.assert_not_called()

            self.assertFalse((danish / 'strings.xml').exists())
            localized = ET.parse(danish / 'custom.xml')
            self.assertEqual(localized.findtext("string[@name='existing']"), 'Existing Danish translation')
            self.assertEqual(localized.findtext("string[@name='refresh']"), 'da: Refresh')
            self.assertEqual([item.text for item in localized.findall('string-array/item')], ['Mandag', 'da: Tuesday'])
            for language in models.TARGET_LANGUAGES:
                folder = res / f'values-{language}'
                self.assertEqual(ET.parse(folder / 'strings_calendar.xml').findtext('string'), f'{language}: Calendar')
                self.assertFalse((folder / 'other.xml').exists())
            self.assertIsNone(ET.parse(res / 'values-fr' / 'strings.xml').find("string[@name='brand']"))
            self.assertNotIn('MissingTranslation', (values / 'strings.xml').read_text())


class WriteBackTests(unittest.TestCase):
    def test_partial_language_report_retains_source_marker(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'values' / 'strings.xml'
            source.parent.mkdir()
            original = '<resources xmlns:tools="http://schemas.android.com/tools"><string name="hello" tools:ignore="MissingTranslation, Typos">Hello</string></resources>'
            source.write_text(original)
            report = Path(tmp) / 'translated.json'
            report.write_text(json.dumps([{'file': str(source), 'language': 'da',
                                          'translations': [{'name': 'hello', 'value': 'Hej', 'context': ''}]}]))
            with contextlib.redirect_stdout(io.StringIO()):
                workflows.run_write_back(report, dry_run=True)
            self.assertEqual(source.read_text(), original)
            self.assertFalse((Path(tmp) / 'values-da').exists())
            with contextlib.redirect_stdout(io.StringIO()):
                workflows.run_write_back(report)
            self.assertEqual(source.read_text(), original)
            self.assertEqual(ET.parse(Path(tmp) / 'values-da' / 'strings.xml').find('string').text, 'Hej')

    def test_collection_updates_keep_other_items_and_update_metadata(self):
        soup = BeautifulSoup('<resources xmlns:tools="http://schemas.android.com/tools">'
                             '<plurals name="count" tools:ignore="MissingTranslation, Typos">'
                             '<item quantity="one">one</item><item quantity="other">old</item></plurals>'
                             '<string-array name="days"><item>old</item><item>keep</item></string-array>'
                             '</resources>', 'xml')
        self.assertEqual(android_xml.upsert_plurals(soup, soup.resources, [
            {'name': 'count', 'quantity': 'other', 'value': 'new', 'context': 'A count'}]), 'updated')
        self.assertEqual(android_xml.upsert_string_array(soup, soup.resources, [
            {'name': 'days', 'index': 0, 'value': 'new', 'context': 'Days'}]), 'updated')
        self.assertEqual(soup.plurals['tools:ignore'], 'Typos')
        self.assertEqual(soup.plurals.find('item', quantity='one').text, 'one')
        self.assertEqual([item.text for item in soup.find('string-array').find_all('item')], ['new', 'keep'])
        self.assertEqual(android_xml.comment_above(soup.plurals), 'A count')
        self.assertEqual(android_xml.upsert_plurals(soup, soup.resources, [
            {'name': 'count', 'quantity': 'other', 'value': 'new', 'context': 'A count'}]), 'unchanged')


if __name__ == "__main__":
    unittest.main()
