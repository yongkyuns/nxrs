import unittest
from paired_transport_report import analyze_pairs, validate_pair_policy
from test_transport_pipeline_report import transcript

def command():
    parts = []
    for batch in range(4):
        parts.append(f'CQ_PAIR_BEGIN batch={batch} first={batch%2}\n')
        for language in (('c', 'rust') if batch%2==0 else ('rust', 'c')):
            parts.append(transcript(language,28,96000000 if language=='c' else 100800000))
    return ''.join(parts) + 'CQ_PAIR_PASS batches=4 messages=5760 threads=20 queues=60\n'

class PairedReportTests(unittest.TestCase):
    def test_policy_rejects_mismatched_payload_optimization_or_provider(self):
        features = ['mq-backend','paired-scale-control','synchronized-scale','native-scale-worker',
                    'shared-mq-code','borrowed-mq-io','ffi-scale-entry','packet-service']
        definitions = '-DNXRS_CQ_DEVICE -DNXRS_CQ_SYNCHRONIZED -DNXRS_CQ_EMBED_CONTROL -DNXRS_CQ_SPEED -DNXRS_CQ_PACKET_SERVICE -DNXRS_PAYLOAD_C_SPEED'
        build = dict(features=features, app_opt_level='2', std_source=dict(selected_source_matches=True),
                     make_command=['NXRS_TARGET_C_FLAGS='+definitions])
        validate_pair_policy(build, 28)
        with self.assertRaises(ValueError): validate_pair_policy(build, 16)
        build['app_opt_level'] = 'z'
        with self.assertRaises(ValueError): validate_pair_policy(build, 28)
        build['app_opt_level'] = '2'
        build['make_command'] = ['NXRS_TARGET_C_FLAGS='+definitions.replace('-DNXRS_CQ_SPEED','')]
        with self.assertRaises(ValueError): validate_pair_policy(build, 28)

    def test_reports_per_command_not_every_batch_as_independent(self):
        result = analyze_pairs(command()*3, 3, 28)
        self.assertEqual(result['runs'], 3)
        self.assertEqual(len(result['pairs']), 12)
        self.assertEqual(result['c_us']['median'], 400000)
        self.assertEqual(result['paired_rust_over_c']['median'], 1.05)

    def test_rejects_wrong_order_missing_duplicate_failed_and_wrong_layout(self):
        valid = command()
        bad = [valid.replace('first=1', 'first=0'), valid.replace('ack_bytes=28','ack_bytes=16'),
               valid.replace('language=c', 'language=rust',1), valid + valid,
               valid.replace('CQ_PAIR_BEGIN batch=3 first=1','CQ_PAIR_BEGIN batch=2 first=0'),
               valid + 'CQ_PAIR_FAIL reason=1\n', valid.replace('cycles=96000000','cycles=0')]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError): analyze_pairs(text,1,28)

    def test_completions_must_delimit_each_command_not_just_match_total_count(self):
        end = 'CQ_PAIR_PASS batches=4 messages=5760 threads=20 queues=60\n'
        wrong = command().replace(end, '') * 2 + end * 2
        with self.assertRaises(ValueError): analyze_pairs(wrong, 2, 28)

if __name__ == '__main__': unittest.main()
