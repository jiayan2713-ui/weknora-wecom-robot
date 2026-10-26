import unittest
from docflow.common import WorkflowError
from parse_policy import classify, policy_signature


class ParsePolicyTests(unittest.TestCase):
    def test_authentication_stops_while_rate_limit_and_server_failure_retry(self):
        for status in (401, 403):
            failure = classify(WorkflowError('MODEL_HTTP_ERROR', 'private diagnostic', scope={'http_status': status}))
            self.assertFalse(failure.retryable)
            self.assertEqual(failure.category, 'credentials')
            self.assertNotIn('private diagnostic', str(failure.record()))
        for status in (408, 429, 503):
            self.assertTrue(classify({'code': 'MODEL_HTTP_ERROR', 'scope': {'http_status': status}}).retryable)

    def test_capacity_and_content_rejection_do_not_retry_even_if_http_is_429(self):
        for category in ('capacity', 'content_rejected'):
            self.assertFalse(classify({'code': 'MODEL_HTTP_ERROR',
                                      'scope': {'http_status': 429, 'category': category}}).retryable)
        self.assertEqual(classify(ValueError('xlsx_visible_extent_exceeds_compaction_bound')).category, 'capacity')

    def test_truncated_output_and_attempt_exhaustion_stop(self):
        for code in ('MODEL_TRUNCATED', 'MODEL_RESPONSE_INVALID', 'ATTEMPTS_EXHAUSTED', 'INTEGRITY_ERROR'):
            self.assertFalse(classify({'code': code}).retryable)

    def test_policy_signature_excludes_credentials_but_changes_with_parser_config(self):
        cfg = {'parser_config': {'model': 'existing-model'}, 'api_key': 'first'}
        before = policy_signature(cfg)
        cfg['api_key'] = 'second'
        self.assertEqual(before, policy_signature(cfg))
        cfg['parser_config']['max_tokens'] = 2000
        self.assertNotEqual(before, policy_signature(cfg))


if __name__ == '__main__':
    unittest.main()
