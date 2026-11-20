# SPDX-License-Identifier: GPL-3.0-or-later
"""Unicorn harness: runs the stock USB interrupt handler against a register-level model of
the AT32/STM32F1-style USB FS device peripheral plus a minimal host.

The model is deliberately strict about what the host may do (a transaction only happens when
the endpoint status is VALID), so a class that forgets to arm an endpoint shows up as a NAK.
"""
import struct

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_WRITE, UC_HOOK_CODE, \
    UC_HOOK_MEM_UNMAPPED, UC_PROT_READ, UC_PROT_EXEC
from unicorn import arm_const
from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1, \
    UC_ARM_REG_R2, UC_ARM_REG_R3

# What the fpga task must find unchanged when it comes back for its next command: r4-r11,
# sp and the VFP registers it keeps constants in (s16-s31, callee-saved).
TASK_REGS = tuple(getattr(arm_const, 'UC_ARM_REG_R%d' % i) for i in range(4, 12)) + (UC_ARM_REG_SP,) + \
    tuple(getattr(arm_const, 'UC_ARM_REG_S%d' % i) for i in range(16, 32))

APP_BASE = 0x08007000
PERIPH_BASE = 0x40000000
USB_BASE = 0x40005C00
PMA_BASE = 0x40006000
SPI3_STS = 0x40003C08
SPI3_DT = 0x40003C0C
USART2_STS = 0x40004400
USART2_DT = 0x40004404
USART2_CTRL1 = 0x4000440C
USART2_IRQ = 0x0802E7B4
X_QUEUE_RECEIVE = 0x080421D8
X_QUEUE_GIVE_FROM_ISR = 0x0804209C
V_TASK_DELAY = 0x08041390
FPGA_TASK = 0x0803E454
UDEV = 0x20002B24
USB_IRQ = 0x0802E8E4
EPT_DEFAULT_INIT = 0x08040990
SYSTICK_HANDLER = 0x0802A994
X_TICK_COUNT = 0x200062D4
UX_SCHEDULER_SUSPENDED = 0x200061E4
SYSTEM_MODE = 0x20001060
SCATTERLOAD = 0x0800718C
SCATTERLOAD_DONE = 0x080072F0
RET_MAGIC = 0x08000100          # execution stops here (mapped, never executed)
STACK_TOP = 0x20036F00

STAT_DIS, STAT_STALL, STAT_NAK, STAT_VALID = 0, 1, 2, 3


def initial_ram(image):
    """SRAM as stock's startup leaves it: run the image's own scatter-load (it decompresses
    .data and zeroes .bss) and return the first 224 KB."""
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    mu.mem_map(0x08000000, 0x100000)
    mu.mem_write(APP_BASE, image)
    mu.mem_map(0x20000000, 0x40000)
    mu.reg_write(UC_ARM_REG_SP, 0x20036F90)
    mu.emu_start(SCATTERLOAD | 1, SCATTERLOAD_DONE, count=50_000_000)
    if mu.reg_read(UC_ARM_REG_PC) != SCATTERLOAD_DONE:
        raise RuntimeError('scatter-load did not finish')
    return bytes(mu.mem_read(0x20000000, 0x38000))


class Stall(Exception):
    pass


class Nak(Exception):
    pass


class UsbEmu:
    def __init__(self, image, ram_init, trace=False):
        self.trace = trace
        self.mu = mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        mu.mem_map(0x08000000, 0x100000)
        mu.mem_write(0x08000000, b'\xff' * 0x100000)
        mu.mem_write(APP_BASE, image)
        mu.mem_protect(0x08000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
        mu.mem_map(0x20000000, 0x40000)
        mu.mem_write(0x20000000, ram_init)
        mu.mem_map(0x1FFFF000, 0x1000)       # UID / option bytes page
        # All APB peripherals are true MMIO: callbacks, no backing memory. (Patching memory
        # from a read hook instead corrupts Unicorn's IT-block state.) USB, SPI3 and USART2
        # are modelled; every other register just remembers what was written to it.
        mu.mmio_map(PERIPH_BASE, 0x30000, self._mmio_read, None, self._mmio_write, None)
        self.regs = {}
        self.spi_rx = 0xFF                   # last byte clocked in on SPI3
        self.spi_count = 0                   # bytes clocked since reset
        self.spi_miso = lambda n: 0xFF       # byte the FPGA answers for the n-th clock
        self.uart_rx = None                  # byte waiting in USART2 DT, or None
        self.stubs = {}
        mu.mem_map(0xE0000000, 0x100000)     # SCS
        self.ep = [0] * 8
        self.istr_flags = 0                  # bits 8..14 only
        self.cntr = 0x0003                   # reset value: FRES + PDWN
        self.daddr = 0
        self.btable = 0
        self.cfg = 0x0002                    # AT32 CFG: pull-up disabled until connect
        self.pma = {}
        self.writes = []                     # (addr, size, value) outside flash/usb, for audits
        self.audit = False
        mu.hook_add(UC_HOOK_MEM_WRITE, self._audit_wr, begin=0x20000000, end=0x2003FFFF)
        mu.hook_add(UC_HOOK_MEM_UNMAPPED, self._unmapped)

    # ---- memory helpers -------------------------------------------------------------------
    def r8(self, a): return self.mu.mem_read(a, 1)[0]
    def r16(self, a): return struct.unpack('<H', self.mu.mem_read(a, 2))[0]
    def r32(self, a): return struct.unpack('<I', self.mu.mem_read(a, 4))[0]
    def w8(self, a, v): self.mu.mem_write(a, bytes([v & 0xFF]))
    def w32(self, a, v): self.mu.mem_write(a, struct.pack('<I', v & 0xFFFFFFFF))

    def _unmapped(self, mu, access, addr, size, value, user):
        raise RuntimeError('unmapped access %08X size %d pc %08X' % (addr, size, mu.reg_read(UC_ARM_REG_PC)))

    def _audit_wr(self, mu, access, addr, size, value, user):
        if self.audit:
            self.writes.append((addr, size, value & ((1 << (8 * size)) - 1), mu.reg_read(UC_ARM_REG_PC)))

    # ---- USB register model ---------------------------------------------------------------
    def _istr(self):
        v = self.istr_flags
        for n in range(8):
            if self.ep[n] & 0x8080:
                v |= 0x8000 | n | (0x10 if self.ep[n] & 0x8000 else 0)
                break
        return v

    def _reg_read(self, off):
        if off < 0x20:
            return self.ep[off // 4]
        return {0x40: self.cntr, 0x44: self._istr(), 0x48: 0, 0x4C: self.daddr, 0x50: self.btable,
                0x60: self.cfg}.get(off, 0)

    def _mmio_read(self, mu, offset, size, user):
        addr = PERIPH_BASE + offset
        base = addr & ~3
        if PMA_BASE <= base < PMA_BASE + 0x400:
            word = self.pma.get((base - PMA_BASE) // 4, 0)
        elif USB_BASE <= base < USB_BASE + 0x64:
            word = self._reg_read(base - USB_BASE)
        elif base == SPI3_STS:
            word = 0x0003                               # RDBF | TDBE: never busy
        elif base == SPI3_DT:
            word = self.spi_rx
        elif base == USART2_STS:
            word = 0x00C0 | (0x20 if self.uart_rx is not None else 0)
        elif base == USART2_DT:
            word = self.uart_rx if self.uart_rx is not None else 0
            self.uart_rx = None                         # reading DT clears RDBF
        else:
            word = self.regs.get(base, 0)
        return (word >> (8 * (addr & 3))) & ((1 << (8 * size)) - 1)

    def _mmio_write(self, mu, offset, size, value, user):
        addr = PERIPH_BASE + offset
        value &= (1 << (8 * size)) - 1
        if not (USB_BASE <= addr < PMA_BASE + 0x400):
            base = addr & ~3
            if base == SPI3_DT:
                self.spi_rx = self.spi_miso(self.spi_count) & 0xFF
                self.spi_count += 1
            elif base not in (SPI3_STS, USART2_STS, USART2_DT):
                shift = 8 * (addr & 3)
                mask = ((1 << (8 * size)) - 1) << shift
                self.regs[base] = (self.regs.get(base, 0) & ~mask) | (value << shift)
            return
        if addr >= PMA_BASE:
            if addr & 3:
                raise RuntimeError('unaligned PMA write %08X' % addr)
            self.pma[(addr - PMA_BASE) // 4] = value & 0xFFFF
            return
        off = addr - USB_BASE
        if off & 3:
            raise RuntimeError('unaligned USB reg write %08X' % addr)
        v = value & 0xFFFF
        if off < 0x20:
            n = off // 4
            old = self.ep[n]
            new = (v & 0x070F)                              # EP_TYPE, EP_KIND, EA
            new |= (old & v) & 0x8080                       # CTR_RX / CTR_TX: write 0 clears
            new |= (old ^ v) & 0x7070                       # DTOG/STAT: write 1 toggles
            if new & 0x8000:
                new |= old & 0x0800                         # SETUP stays while CTR_RX is set
            self.ep[n] = new
            if self.trace:
                print('    EP%dR <- %04X => %04X' % (n, v, new))
        elif off == 0x40:
            self.cntr = v
        elif off == 0x44:
            self.istr_flags &= v & 0x7F00
        elif off == 0x4C:
            self.daddr = v
        elif off == 0x50:
            self.btable = v
        elif off == 0x60:
            self.cfg = v

    # ---- running firmware code ------------------------------------------------------------
    def call(self, addr, *args, limit=2_000_000):
        mu = self.mu
        for reg, val in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            mu.reg_write(reg, val & 0xFFFFFFFF)
        mu.reg_write(UC_ARM_REG_SP, STACK_TOP)
        mu.reg_write(UC_ARM_REG_LR, RET_MAGIC | 1)
        mu.emu_start(addr | 1, RET_MAGIC, count=limit)
        pc = mu.reg_read(UC_ARM_REG_PC)
        if pc != RET_MAGIC:
            raise RuntimeError('call %08X did not return (pc %08X)' % (addr, pc))
        if mu.reg_read(UC_ARM_REG_SP) != STACK_TOP:
            raise RuntimeError('call %08X unbalanced stack' % addr)
        return mu.reg_read(UC_ARM_REG_R0)

    def irq(self):
        self.call(USB_IRQ)

    # ---- stubs for RTOS calls that cannot run without a scheduler --------------------------
    def stub(self, addr, fn):
        """Replace the function at addr: fn(emu) runs instead and its return value goes to r0."""
        def hook(mu, address, size, user):
            r = fn(self)
            if r is not None:
                mu.reg_write(UC_ARM_REG_R0, r & 0xFFFFFFFF)
            mu.reg_write(UC_ARM_REG_PC, mu.reg_read(UC_ARM_REG_LR) | 1)
        self.stubs[addr] = self.mu.hook_add(UC_HOOK_CODE, hook, begin=addr, end=addr)

    # ---- the meter's serial port ------------------------------------------------------------
    def uart_byte(self, byte):
        """Deliver one byte from the meter chip: runs stock's USART2 interrupt handler."""
        if X_QUEUE_GIVE_FROM_ISR not in self.stubs:
            self.stub(X_QUEUE_GIVE_FROM_ISR, lambda e: 0)
        self.regs[USART2_CTRL1] = 0x2020                # enabled, receive interrupt on
        self.uart_rx = byte & 0xFF
        self.call(USART2_IRQ)
        if self.uart_rx is not None:
            raise RuntimeError('USART2 handler did not read the byte')

    # ---- the scope acquisition task -----------------------------------------------------------
    def run_fpga_task(self, opcodes, stack_top=0x20035F00):
        """Run stock's `fpga` task from its entry and feed it this list of SPI3 commands.
        Returns the register snapshot taken each time the task asks for its next command."""
        mu = self.mu
        pending = list(opcodes)
        snaps = []

        def receive(e):
            snaps.append(tuple(mu.reg_read(r) for r in TASK_REGS))
            if not pending:
                mu.emu_stop()
                return None
            buf = mu.reg_read(UC_ARM_REG_R1)
            e.w8(buf, pending.pop(0))
            return 1

        for addr in (X_QUEUE_RECEIVE, V_TASK_DELAY):
            if addr in self.stubs:
                mu.hook_del(self.stubs.pop(addr))
        self.stub(X_QUEUE_RECEIVE, receive)
        self.stub(V_TASK_DELAY, lambda e: None)
        # A task starts with whatever is in the registers; make that the same for every run
        # so two images can be compared.
        for i in range(13):
            mu.reg_write(getattr(arm_const, 'UC_ARM_REG_R%d' % i), 0)
        for i in range(32):
            mu.reg_write(getattr(arm_const, 'UC_ARM_REG_S%d' % i), 0)
        mu.reg_write(UC_ARM_REG_SP, stack_top)
        mu.reg_write(UC_ARM_REG_LR, RET_MAGIC | 1)
        mu.emu_start(FPGA_TASK | 1, RET_MAGIC, count=20_000_000)
        if pending:
            raise RuntimeError('fpga task stopped early at pc %08X, %d commands left'
                               % (mu.reg_read(UC_ARM_REG_PC), len(pending)))
        return snaps

    def tick(self, n=1):
        """Run the (hooked) SysTick handler n times. The scheduler is marked suspended so
        xTaskIncrementTick only pends the tick; xTickCount is advanced here instead."""
        self.w32(UX_SCHEDULER_SUSPENDED, 1)
        for _ in range(n):
            self.w32(X_TICK_COUNT, self.r32(X_TICK_COUNT) + 1)
            self.call(SYSTICK_HANDLER)

    # ---- what stock master_init does for USB (0x0802CB86..0x0802CBF8) ----------------------
    def stock_usb_init(self):
        self.w32(UDEV + 0, USB_BASE)
        self.w32(UDEV + 4, 0x20000030)
        self.w32(UDEV + 8, 0x20000058)
        self.call(EPT_DEFAULT_INIT)
        self.cntr &= ~1
        self.istr_flags = 0
        self.btable = 0
        self.daddr = 0x80
        self.cntr |= 0x9E00
        self.cntr &= ~2
        self.cfg &= ~2
        # ...and immediately disconnects again
        self.cntr |= 2
        self.cfg |= 2

    def connected(self):
        return not (self.cntr & 2) and not (self.cfg & 2)

    # ---- host side ------------------------------------------------------------------------
    def _bt(self, n, field):
        return self.pma.get((self.btable + 8 * n + field) // 2, 0)

    def _bt_set(self, n, field, v):
        self.pma[(self.btable + 8 * n + field) // 2] = v & 0xFFFF

    def _pma_read(self, off, n):
        out = bytearray()
        for i in range(0, n, 2):
            w = self.pma.get((off + i) // 2, 0)
            out += bytes([w & 0xFF, w >> 8])
        return bytes(out[:n])

    def _pma_write(self, off, data):
        data = bytes(data) + (b'\x00' if len(data) & 1 else b'')
        for i in range(0, len(data), 2):
            self.pma[(off + i) // 2] = data[i] | (data[i + 1] << 8)

    def _stat_rx(self, n): return (self.ep[n] >> 12) & 3
    def _stat_tx(self, n): return (self.ep[n] >> 4) & 3

    def _need_connected(self):
        if not self.connected():
            raise RuntimeError('host: device is not connected (CNTR %04X CFG %04X)' % (self.cntr, self.cfg))

    def bus_reset(self):
        self._need_connected()
        self.ep = [0] * 8
        self.daddr = 0
        self.istr_flags |= 0x0400
        self.irq()

    def sof(self):
        self._need_connected()
        self.istr_flags |= 0x0200
        self.irq()

    def host_out(self, n, data, setup=False, run_irq=True):
        self._need_connected()
        if not setup and self._stat_rx(n) != STAT_VALID:
            raise (Stall if self._stat_rx(n) == STAT_STALL else Nak)('OUT ep%d stat_rx=%d' % (n, self._stat_rx(n)))
        if setup and self._stat_rx(n) == STAT_DIS:
            raise RuntimeError('SETUP while ep0 RX disabled')
        self._pma_write(self._bt(n, 4), data)
        self._bt_set(n, 6, (self._bt(n, 6) & 0xFC00) | len(data))
        e = self.ep[n]
        e |= 0x8000
        e = (e & ~0x3000) | (STAT_NAK << 12)
        if setup:
            e |= 0x0800
            e = (e & ~0x0030) | (STAT_NAK << 4)
        else:
            e &= ~0x0800
            e ^= 0x4000
        self.ep[n] = e
        if run_irq:
            self.irq()

    def host_in(self, n):
        self._need_connected()
        st = self._stat_tx(n)
        if st != STAT_VALID:
            raise (Stall if st == STAT_STALL else Nak)('IN ep%d stat_tx=%d' % (n, st))
        cnt = self._bt(n, 2) & 0x3FF
        data = self._pma_read(self._bt(n, 0), cnt)
        e = self.ep[n]
        e |= 0x0080
        e = (e & ~0x0030) | (STAT_NAK << 4)
        e ^= 0x0040
        self.ep[n] = e
        self.irq()
        return data

    def control(self, bm, req, val, idx, length, data_out=b''):
        setup = struct.pack('<BBHHH', bm, req, val, idx, length)
        self.host_out(0, setup, setup=True)
        if bm & 0x80:
            got = b''
            while len(got) < length:
                pkt = self.host_in(0)
                got += pkt
                if len(pkt) < 64:
                    break
            self.host_out(0, b'')            # status stage
            return got[:length]
        pos = 0
        while pos < len(data_out):
            self.host_out(0, data_out[pos:pos + 64])
            pos += 64
        z = self.host_in(0)                  # status stage
        if z:
            raise RuntimeError('status IN not zero length')
        return b''

    def get_descriptor(self, dtype, index=0, length=255, lang=0, recipient=0):
        return self.control(0x80 | recipient, 6, (dtype << 8) | index, lang, length)

    def enumerate(self):
        """Typical host sequence. Returns a dict with what the device reported."""
        self.bus_reset()
        dev8 = self.get_descriptor(1, length=8)
        self.bus_reset()
        self.control(0x00, 5, 7, 0, 0)       # SET_ADDRESS 7
        if (self.daddr & 0x7F) != 7:
            raise RuntimeError('device did not take address (DADDR %04X)' % self.daddr)
        dev = self.get_descriptor(1, length=18)
        cfg9 = self.get_descriptor(2, length=9)
        total = struct.unpack_from('<H', cfg9, 2)[0]
        cfg = self.get_descriptor(2, length=total)
        info = {'dev8': dev8, 'device': dev, 'config': cfg, 'strings': {}}
        lang = self.get_descriptor(3, 0)
        info['langid'] = lang
        for i in sorted(set(dev[14:17]) - {0}):
            s = self.get_descriptor(3, i, lang=0x0409)
            info['strings'][i] = s[2:s[0]].decode('utf-16le', 'replace')
        self.control(0x00, 9, cfg[5], 0, 0)  # SET_CONFIGURATION
        return info
