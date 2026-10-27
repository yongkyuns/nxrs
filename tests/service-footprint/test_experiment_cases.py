"""Validate the closed Rust case contracts without an SDK or device."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

class ExperimentCaseTests(unittest.TestCase):
    def test_selected_cases_preserve_protocols_and_reject_invalid_combinations(self):
        source = f'''
#[path = "{ROOT / 'tests/service-footprint/src/experiment.rs'}"] mod footprint;
#[path = "{ROOT / 'tests/event-services-comparison/experiment.rs'}"] mod event;
#[test] fn footprint_contract() {{
    use footprint::{{Experiment, Workload, Io}};
    let owned = Experiment::parse("rust-owned-z").unwrap();
    assert_eq!((owned.workload,owned.io,owned.paired),(Workload::Wire,Io::Owned,false));
    let copied = Experiment::parse("rust-packet-2").unwrap();
    let inplace = Experiment::parse("rust-packet-inplace-2").unwrap();
    assert_eq!((copied.workload.packet(),inplace.workload.packet()),(true,true));
    assert!(!copied.workload.in_place() && inplace.workload.in_place());
    assert!(Experiment::parse("paired-packet-inplace").unwrap().paired);
    for bad in ["unknown","wire-only","packet+wire","borrowed-mq-io"] {{
        assert!(Experiment::parse(bad).is_err());
    }}
}}
#[test] fn event_contract() {{
    use event::{{Experiment,Scheduling,Layout,Work}};
    let c=Experiment::parse("mailbox-1ms-budget-chunked").unwrap();
    assert_eq!((c.layout,c.timer_ms,c.scheduling,c.work),(Layout::Mailbox,1,Scheduling::Budget,Work::Chunked));
    for bad in ["mailbox-1ms-natural-chunked","lanes-2ms-event-monolithic","unknown"] {{
        assert!(Experiment::parse(bad).is_err());
    }}
}}
'''
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'cases'
            subprocess.run(['rustc','--edition=2024','--test','-Adead_code','-',
                            '-o',str(binary)],input=source,text=True,check=True,capture_output=True)
            subprocess.run([str(binary)],check=True,capture_output=True)
