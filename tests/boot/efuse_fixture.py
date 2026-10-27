"""Test-only actual Espressif virtual read-register fixtures; no serial or burns."""
import contextlib
import io
from espefuse.efuse.esp32s3 import EspEfuses, EmulateEfuseController, commands
from espefuse.efuse.esp32s3.mem_definition import EfuseDefineFields

MAC = '02:00:12:34:56:78'


class Capture(io.StringIO):
    name = 'private-virtual-summary'
    def close(self):
        self.raw = self.getvalue().encode()
        super().close()


def virtual_summary(changes=None, protected=(), hidden=()):
    # Instantiate the actual SDK's memory-only controller directly. This cannot
    # select a serial transport. Inject read-register fixtures, not burn commands.
    with contextlib.redirect_stdout(io.StringIO()):
        controller = EmulateEfuseController()
        definitions = EfuseDefineFields(None)
        fields = {f.name: f for f in definitions.EFUSES + definitions.KEYBLOCKS + definitions.CALIBRATION_EFUSES}
        changes = dict(changes or {})
        changes['MAC'] = int(MAC.replace(':', ''), 16)
        for name in protected:
            changes['WR_DIS'] = changes.get('WR_DIS', 0) | (1 << fields[name].write_disable_bit)
        for name in hidden:
            changes['RD_DIS'] = changes.get('RD_DIS', 0) | (1 << fields[name].read_disable_bit)
        for name, value in changes.items():
            field = fields[name]
            assert 0 <= value < 1 << field.bit_len
            shifted = value << field.pos
            for i in range((field.pos + field.bit_len + 31)//32):
                controller.direct_write_efuse(field.word + i, (shifted >> (32*i)) & 0xffffffff, field.block)
        reader = EspEfuses(controller)
        operation = commands()
        operation.esp = controller
        operation.efuses = reader
        output = Capture()
        operation.summary(format='json', file=output)
    return output.raw


def rooted(digest, enabled=True, **kwargs):
    changes = dict(BLOCK_KEY0=int.from_bytes(digest, 'little'), KEY_PURPOSE_0=9)
    protected = ['BLOCK_KEY0', 'KEY_PURPOSE_0']
    if enabled:
        changes.update(SECURE_BOOT_EN=1, SECURE_BOOT_KEY_REVOKE1=1, SECURE_BOOT_KEY_REVOKE2=1)
        protected.append('RD_DIS')
    changes.update(kwargs.pop('changes', {}))
    return virtual_summary(changes, protected=kwargs.pop('protected', protected), **kwargs)
