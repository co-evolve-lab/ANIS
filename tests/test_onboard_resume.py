import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from anis import onboard


class ImplementationResumeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.stage_root = self.root / 'release'
        self.config = {'app_aliases': {}}
        for name, value in [('ROOT', str(self.root)),
                            ('STAGE2_DIR', str(self.stage_root)),
                            ('ACTIVE_CONFIG', self.config)]:
            patch = mock.patch.object(onboard, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def outcomes(self, planned=('first', 'second'), completed=(), failed=(),
                 run_id='current-run', universe=None):
        folder = self.stage_root / 'runs' / run_id / 'example'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'manifest.json').write_text(json.dumps({
            'run_id': run_id, 'app': 'example',
            'operations': [{'command': name} for name in planned]}))
        (folder / 'state.json').write_text(json.dumps({
            'command_universe': list(planned if universe is None else universe),
            'completed': list(completed),
            'failed_commands': {name: {'attempts': 2, 'reason': 'Runtime failed'}
                                for name in failed}}))
        return folder

    def ready(self):
        return onboard.check_accepted_implemented('example', 'current-run')

    def test_missing_work_order_invokes_implementation(self):
        calls = []

        def implement():
            calls.append('implementation')
            self.outcomes(completed=('first', 'second'))
            return True, 'Done'

        stage = {'name': 'Implement', 'owner': 'Model',
                 'check': self.ready, 'run': implement}
        with contextlib.redirect_stdout(io.StringIO()):
            passed, _ = onboard.run_stage(stage, 'example', '', 1, self.config)
        self.assertTrue(passed)
        self.assertEqual(calls, ['implementation'])

    def test_pending_commands_are_not_ready(self):
        self.outcomes(completed=('first',))
        passed, detail = self.ready()
        self.assertFalse(passed)
        self.assertIn('second', detail)

    def test_recorded_failures_allow_partial_implementation(self):
        self.outcomes(completed=('first',), failed=('second',))
        passed, detail = self.ready()
        self.assertTrue(passed)
        self.assertIn('1 accepted, 1 failed', detail)

    def test_other_run_cannot_satisfy_current_run(self):
        self.outcomes(completed=('first', 'second'), run_id='older-run')
        self.assertFalse(self.ready()[0])

    def test_manifest_without_execution_is_not_ready(self):
        folder = self.outcomes()
        (folder / 'state.json').unlink()
        self.assertFalse(self.ready()[0])

    def test_shrunk_work_order_retains_unfinished_universe(self):
        self.outcomes(planned=('second',), completed=('second',),
                      universe=('first', 'second'))
        self.assertFalse(self.ready()[0])

    def test_malformed_outcomes_require_work(self):
        folder = self.outcomes()
        (folder / 'state.json').write_text('{}')
        self.assertFalse(self.ready()[0])

    def phases(self, calls, fail=False):
        def implement():
            calls.append('implement')
            if fail:
                return False, 'Interrupted'
            self.outcomes(completed=('first', 'second'))
            return True, 'Done'

        def run(name):
            return lambda: (calls.append(name) or True, 'Done')

        return [{'name': 'Workflow', 'steps': [
            {'name': 'Discover', 'owner': 'Model', 'check': lambda: (True, 'Ready'),
             'run': run('discover')},
            {'name': 'Filter', 'owner': 'Model', 'check': lambda: (True, 'Ready'),
             'run': run('filter')},
            {'name': 'Implement', 'owner': 'Model', 'check': self.ready,
             'recheck_on_resume': True, 'run': implement},
            {'name': 'Verify', 'owner': 'Script', 'check': lambda: (True, 'Old report'),
             'run': run('verify')},
            {'name': 'Package', 'owner': 'Script', 'check': lambda: (True, 'Old package'),
             'run': run('package')},
        ]}]

    def old_state(self):
        return {'app': 'example', 'workflow_run_id': 'current-run',
                'done': ['Discover', 'Filter', 'Implement', 'Verify', 'Package'],
                'history': []}

    def resume(self, calls, state, fail=False):
        with mock.patch.object(onboard, 'workflow_stages',
                               return_value=self.phases(calls, fail)), \
                contextlib.redirect_stdout(io.StringIO()):
            return onboard.onboard('example', '', config=self.config,
                                   state=state, max_attempts=1)

    def test_false_completion_reuses_filtering_and_refreshes_outputs(self):
        calls = []
        state = self.resume(calls, self.old_state())
        self.assertEqual(calls, ['implement', 'verify', 'package'])
        self.assertEqual(state['done'], self.old_state()['done'])
        self.assertEqual(state['rerun_steps'], [])
        self.assertEqual(state['history'][0]['invalidated_steps'],
                         ['Implement', 'Verify', 'Package'])

    def test_interrupted_recovery_persists_refresh_for_next_resume(self):
        with self.assertRaises(onboard.StageFailed):
            self.resume([], self.old_state(), fail=True)
        saved = onboard.load_state('example')
        self.assertEqual(saved['done'], ['Discover', 'Filter'])
        self.assertEqual(saved['rerun_steps'], ['Implement', 'Verify', 'Package'])
        calls = []
        self.resume(calls, saved)
        self.assertEqual(calls, ['implement', 'verify', 'package'])

    def test_completed_implementation_is_not_restarted(self):
        self.outcomes(completed=('first', 'second'))
        calls = []
        self.resume(calls, self.old_state())
        self.assertEqual(calls, [])

    def test_zero_passed_is_rejected_but_partial_success_is_supported(self):
        folder = self.root / 'apps' / 'example' / 'build'
        folder.mkdir(parents=True)
        (folder / 'report.json').write_text(json.dumps({'first': {'pass': False}}))
        self.assertFalse(onboard.check_verify_report('example', 1.0)[0])
        self.assertFalse(onboard.check_verify_report('example', 0.0)[0])
        (folder / 'report.json').write_text(json.dumps({
            'first': {'pass': True}, 'second': {'pass': False}}))
        self.assertTrue(onboard.check_verify_report('example', 1.0)[0])

    def test_empty_release_with_launcher_is_not_ready(self):
        folder = self.root / 'dist' / 'example-anis' / 'commands'
        folder.mkdir(parents=True)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'example-anis').write_text('#!/bin/sh\n')
        self.assertFalse(onboard.check_dist('example')[0])


if __name__ == '__main__':
    unittest.main()
