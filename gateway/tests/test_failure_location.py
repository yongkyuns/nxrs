"""A public error location cannot expose private exception text or locals."""
import json
from pathlib import Path
import unittest
from failure_location import trace_locations,project_locations


class FailureLocationTests(unittest.TestCase):
    def test_chained_errors_expose_only_allowed_source_line_and_fixed_exception_class(self):
        name='gateway/tests/test_failure_location.py';sources={str(Path(__file__).resolve()):name}
        try:
            try:raise ValueError('private-value /private/custody/file')
            except ValueError as error:raise RuntimeError('private outer value') from error
        except RuntimeError as error:locations=trace_locations(error,sources)
        self.assertEqual({v['error'] for v in locations},{'RuntimeError','ValueError'})
        self.assertTrue(all(v['source']==name and type(v['line']) is int for v in locations))
        self.assertNotIn('private',json.dumps(locations));self.assertEqual(project_locations(locations,{name}),locations)
        self.assertEqual(project_locations(locations,{'different'}),[])

    def test_private_exception_names_and_unregistered_code_files_are_withheld(self):
        class PrivateException(Exception):pass
        try:raise PrivateException('private-value')
        except PrivateException as error:
            self.assertEqual(trace_locations(error,{}),[])
            value=trace_locations(error,{str(Path(__file__).resolve()):'gateway/tests/test_failure_location.py'})
        self.assertEqual(value[0]['error'],'Exception');self.assertNotIn('PrivateException',json.dumps(value))

    def test_builtin_import_failures_retain_only_fixed_class_and_source_coordinates(self):
        name='gateway/tests/test_failure_location.py';sources={str(Path(__file__).resolve()):name}
        for kind in (AttributeError,NameError,UnboundLocalError,SyntaxError,IndentationError,TabError,
                     SystemError,MemoryError,RecursionError,EOFError,UnicodeDecodeError):
            error=kind('private-value /private/custody/file') if kind is not UnicodeDecodeError else kind('ascii',b'\xff',0,1,'private-value')
            try:raise error
            except Exception as error:rows=trace_locations(error,sources)
            with self.subTest(kind=kind.__name__):
                self.assertTrue(rows)
                self.assertTrue(all(row['error']==kind.__name__ and row['source']==name for row in rows))
                self.assertNotIn('private',json.dumps(rows))
                self.assertEqual(project_locations(rows,{name}),rows)

    def test_untrusted_location_fields_cannot_export_text_or_break_failure_projection(self):
        good=dict(source='gateway/tests/activity_reference.py',line=42,error='AssertionError')
        self.assertEqual(project_locations([dict(good,secret='private-value')],{good['source']}),[good])
        for field,value in [('source',['private-value']),('source','/private/custody'),('error',{}),('error','private-value'),
                            ('line',True),('line',-1),('line',10001),('line','private-value')]:
            with self.subTest(field=field,value=value):
                self.assertEqual(project_locations([dict(good,**{field:value})],{good['source']}),[])
        for value in [None,{},[good]*9]:self.assertEqual(project_locations(value,{good['source']}),[])


if __name__=='__main__':unittest.main()
