import hashlib
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock


checker = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools/check-armv8m-stack-handover.py"))
check = checker["check_disassembly"]
ContractError = checker["ContractError"]


def fixture(hardware_limits=False, replacements=()):
    # Synthetic GNU objdump text, not an executed or assembled qualification
    # image. The words supply Thumb widths; tests concern decoded instructions.
    rows = [
        ("b570", "push {r4, r5, r6, lr}"),
        ("480f", "ldr r0, [pc, #60]"),
        ("f3ef 8410", "mrs r4, PRIMASK"),
        ("b672", "cpsid i"),
        ("f3ef 8514", "mrs r5, CONTROL"),
        ("f015 0f02", "tst.w r5, #2"),
        ("d103", "bne.n {common} <arm_initialize_stack+0x20>"),
        ("f3ef 8608", "mrs r6, MSP"),
        ("f386 8809", "msr PSP, r6"),
    ]
    if hardware_limits:
        rows += [("f3ef 860a", "mrs r6, MSPLIM"), ("f386 880b", "msr PSPLIM, r6")]
    common = len(rows)
    if hardware_limits:
        rows += [("f04f 0600", "mov.w r6, #0"), ("f386 880a", "msr MSPLIM, r6")]
    rows += [("f380 8808", "msr MSP, r0")]
    if hardware_limits:
        rows += [("f381 880a", "msr MSPLIM, r1")]
    rows += [
        ("f3bf 8f6f", "isb sy"),
        ("f045 0502", "orr.w r5, r5, #2"),
        ("f385 8814", "msr CONTROL, r5"),
        ("f3bf 8f6f", "isb sy"),
        ("f384 8810", "msr PRIMASK, r4"),
        ("bd70", "pop {r4, r5, r6, pc}"),
    ]
    address = 0x1000
    addresses = []
    for words, _ in rows:
        addresses.append(address)
        address += len(words.replace(" ", "")) // 2
    text = "00001000 <arm_initialize_stack>:\n" + "\n".join(
        f" {address:x}:\t{words}\t{assembly.replace('{common}', f'{addresses[common]:x}')}"
        for address, (words, assembly) in zip(addresses, rows)) + "\n"
    for old, new in replacements:
        if old not in text:
            raise AssertionError(f"fixture replacement not found: {old!r}")
        text = text.replace(old, new)
    return text


class Armv8mStackHandoverTest(unittest.TestCase):
    def rejects(self, replacements, message, hardware_limits=False):
        with self.assertRaisesRegex(ContractError, message):
            check(fixture(hardware_limits, replacements), hardware_limits)

    def test_no_limit_handover_has_explicit_static_scope(self):
        result = check(fixture(), False)
        self.assertTrue(result["passed"])
        self.assertFalse(result["runtime_qualified"])
        self.assertEqual(result["instruction_count"], 13)
        self.assertEqual(result["registers"]["saved_primask"], "r4")
        self.assertLess(int(result["msp_install"], 16), int(result["control_select"], 16))

    def test_hardware_limits_branch_reaches_clear_before_msp_install(self):
        result = check(fixture(True), True)
        self.assertEqual(result["instruction_count"], 18)
        self.assertEqual(result["registers"]["irq_stack_limit"], "r1")
        self.assertEqual(int(result["msp_install"], 16) - int(result["already_psp_target"], 16), 8)

    def test_register_allocation_and_gnu_aliases_do_not_change_contract(self):
        text = fixture(True, [("r4", "sl"), ("r5", "fp"), ("r6", "ip"),
                              ("#2", "#0x2"), ("isb sy", "isb #0xf"),
                              ("mov.w", "movs.w"), ("orr.w", "orrs.w")])
        result = check(text, True)
        self.assertEqual(result["registers"]["saved_primask"], "r10")
        self.assertEqual(result["registers"]["saved_control"], "r11")
        self.assertEqual(result["registers"]["temporary"], "r12")

    def test_control_before_msp_recreates_the_unsafe_order_even_with_a_mask(self):
        self.rejects([("msr MSP, r0", "msr CONTROL, r5"),
                      ("orr.w r5, r5, #2", "msr MSP, r0")], "expected msr msp")

    def test_mask_is_required_and_must_cover_configurable_interrupts(self):
        for replacement in ("nop", "cpsid f", "cpsie i", "msr BASEPRI, r0"):
            with self.subTest(replacement=replacement):
                self.rejects([("cpsid i", replacement)], "expected cpsid i")
        self.rejects([("cpsid i", "late-mask-placeholder"),
                      ("mrs r5, CONTROL", "cpsid i"),
                      ("late-mask-placeholder", "mrs r5, CONTROL")], "expected cpsid i")

    def test_missing_save_and_wrong_restore_cannot_claim_mask_preservation(self):
        self.rejects([("mrs r4, PRIMASK", "mov.w r4, #0")], "expected one PRIMASK save")
        self.rejects([("msr PRIMASK, r4", "cpsie i")], "expected msr primask")
        self.rejects([("msr PRIMASK, r4", "msr PRIMASK, r6")], "expected msr primask, r4")

    def test_already_psp_path_must_skip_the_copy_and_nothing_else(self):
        for hardware in (False, True):
            text = fixture(hardware)
            result = check(text, hardware)
            target = result["already_psp_target"][2:]
            for wrong in ("1000", f"{int(target, 16) - 4:x}", f"{int(target, 16) + 4:x}", "2000"):
                with self.subTest(hardware=hardware, wrong=wrong):
                    self.rejects([(f"bne.n {target}", f"bne.n {wrong}")], "BNE must skip only", hardware)
            self.rejects([("bne.n", "beq.n")], "expected bne", hardware)

    def test_tst_and_control_update_preserve_other_control_bits(self):
        for old, new, error in (
            ("tst.w r5, #2", "tst.w r5, #1", "expected tst"),
            ("orr.w r5, r5, #2", "mov.w r5, #2", "expected orr"),
            ("orr.w r5, r5, #2", "orr.w r5, r4, #2", "expected orr"),
            ("msr CONTROL, r5", "msr CONTROL, r6", "expected msr control"),
        ):
            with self.subTest(new=new):
                self.rejects([(old, new)], error)

    def test_control_write_requires_an_immediate_isb(self):
        before, after = fixture().rsplit("isb sy", 1)
        text = before + "nop" + after
        with self.assertRaisesRegex(ContractError, "expected isb"):
            check(text, False)

    def test_handover_cannot_access_the_stack_or_call_a_function(self):
        for instruction in ("push {r4}", "ldr r6, [sp, #0]", "str r6, [sp, #0]",
                            "bl 2000 <helper>"):
            with self.subTest(instruction=instruction):
                self.rejects([("orr.w r5, r5, #2", instruction)], "expected orr")

    def test_hardware_limits_are_copied_cleared_and_reinstalled_in_order(self):
        for old, new, error in (
            ("mrs r6, MSPLIM", "mrs r6, PSPLIM", "expected mrs r6, msplim"),
            ("msr PSPLIM, r6", "msr PSPLIM, r0", "expected msr psplim, r6"),
            ("mov.w r6, #0", "mov.w r6, #1", "expected mov"),
            ("msr MSPLIM, r6", "msr MSP, r0", "expected msr msplim, r6"),
            ("msr MSPLIM, r1", "nop", "expected msr msplim"),
        ):
            with self.subTest(new=new):
                self.rejects([(old, new)], error, True)

    def test_hardware_limit_expectation_is_not_guessed_from_the_image(self):
        for actual, expected in ((True, False), (False, True)):
            with self.subTest(actual=actual):
                with self.assertRaises(ContractError):
                    check(fixture(actual), expected)

    def test_aliased_register_roles_would_destroy_saved_state_or_stack_inputs(self):
        for old, new in (("r6", "r4"), ("r5", "r4"), ("r0", "r6"), ("r1", "r0")):
            with self.subTest(old=old, new=new):
                self.rejects([(old, new)], "register roles must be independent", True)
        self.rejects([("r6", "sp")], "other than SP/PC")

    def test_unchecked_mask_or_stack_writes_outside_block_are_rejected(self):
        self.rejects([("ldr r0, [pc, #60]", "cpsie i")], "outside checked handover")
        self.rejects([("pop {r4, r5, r6, pc}", "msr PSP, r0")], "outside checked handover")

    def test_prefix_cannot_bypass_the_handover_and_suffix_cannot_reenter_it(self):
        for branch in ("b 1032 <return>", "pop {r4, pc}", "mov pc, lr", "it ne"):
            with self.subTest(branch=branch):
                self.rejects([("ldr r0, [pc, #60]", branch)], "control flow before handover")
        self.rejects([("pop {r4, r5, r6, pc}", "b 1006 <arm_initialize_stack+6>")], "branch into checked handover")

    def test_missing_duplicate_or_incomplete_disassembly_fails(self):
        for text, message in (
            (fixture().replace("<arm_initialize_stack>:", "<another_function>:"), "found 0"),
            (fixture() + fixture(), "found 2"),
            (fixture().replace("1008:", "100a:"), "noncontiguous"),
            (fixture().replace("b672\tcpsid", "b6??\tcpsid"), "decoded Thumb instruction"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ContractError, message):
                    check(text, False)

    def test_elf_inspection_only_invokes_objdump_and_binds_receipt_to_bytes(self):
        header = bytearray(52)
        header[:6] = b"\x7fELF\x01\x01"
        header[18:20] = (40).to_bytes(2, "little")
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "synthetic.elf"
            elf.write_bytes(header)

            def disassemble(command, *, stdout, stderr, timeout, check):
                self.assertEqual(command, ["test-objdump", "-d", "--disassemble=arm_initialize_stack", str(elf)])
                self.assertEqual(timeout, 30)
                self.assertFalse(check)
                stdout.write(fixture().encode())
                return mock.Mock(returncode=0)

            with mock.patch.object(checker["subprocess"], "run", side_effect=disassemble) as run:
                receipt = checker["inspect_elf"](elf, "test-objdump", False)
            run.assert_called_once()
            self.assertEqual(receipt["elf_sha256"], hashlib.sha256(header).hexdigest())
            self.assertFalse(receipt["runtime_qualified"])

    def test_host_binary_or_wrong_elf_architecture_never_reaches_objdump(self):
        for header in (b"MZ" + bytes(50), b"\x7fELF\x02\x01" + bytes(46),
                       b"\x7fELF\x01\x01" + bytes(12) + b"\x3e\x00" + bytes(32)):
            with self.subTest(header=header[:6]), tempfile.TemporaryDirectory() as directory:
                elf = Path(directory) / "wrong-image"
                elf.write_bytes(header)
                with mock.patch.object(checker["subprocess"], "run") as run:
                    with self.assertRaisesRegex(ContractError, "ELF32 ARM"):
                        checker["inspect_elf"](elf, "test-objdump", False)
                run.assert_not_called()
