/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * stock-link -- a PC/tablet link that rides inside FNIRSI 2C53T stock firmware V1.2.0.
 *
 * Built by build_link_image.py into a code cave appended to the user's own stock APP image
 * and entered from TWO hooks:
 *   link_tick()       from the FreeRTOS SysTick handler (0x0802A994), every tick;
 *   link_fpga_tail()  from the tail of the `fpga` task's command loop (0x0803E49A), once per
 *                     SPI3 command, in task context.
 *
 * How it works
 *   Stock keeps the USB device peripheral initialised from boot but disconnected, and only
 *   connects it in "USB Sharing" (system mode 7) with a mass-storage class. Its USB core is
 *   the Artery usbd core, which reaches the class and the descriptors through two pointers in
 *   the core structure (0x20002B28 / 0x20002B2C).
 *
 *   link_tick() watches the system mode. While the instrument is in a measurement mode (1..3)
 *   and stock has left the bus disconnected for LINK_SETTLE_TICKS, it points the core at the
 *   HID class below and connects. As soon as the mode is anything else it disconnects and puts
 *   stock's pointers back, so USB Sharing behaves exactly as before.
 *
 *   The HID class carries 64-byte reports:
 *     requests   INFO, READ (MCU flash / SRAM, read-only, range checked), STREAM (subscribe),
 *                WAVE (patch 4: the generator's table of samples, and nothing else);
 *     events     meter frames, oscilloscope records and status blocks, only after the host
 *                subscribed to each.
 *
 *   Meter: the class's SOF callback (1 kHz while a host is attached) watches stock's USART2
 *   receive index. When it wraps after a complete frame, the 12 bytes are still untouched --
 *   the next frame is 160 ms away -- and are copied out. No stock code is hooked for this.
 *
 *   Scope: stock's `fpga` task reads each channel into a 1024-byte buffer (SPI3 commands
 *   0x04 / 0x05) and corrects it in place. link_fpga_tail() runs in that same task right
 *   after a command finishes, so a copy taken there is a complete record by construction.
 *   It only copies while the host is subscribed.
 *
 *   Status (patch 3): a few things stock keeps in SRAM and shows on its screen but that are in
 *   neither stream -- the generator's settings, the scope's own measurements with its
 *   run/stop flag, the rest of the meter's state. Every STATUS_PERIOD_TICKS the SOF callback
 *   queues the next block that applies to the current mode; it is copied when the report is
 *   built, in the USB interrupt, so each block is one instant. Nothing is hooked for this.
 *
 *   Generator (patch 4): stock plays a table of 100 samples in SRAM through a circular DMA into
 *   the DAC, and rewrites it only when a generator setting changes. WAVE stores samples there,
 *   scaled the way stock scales its own (sample * amplitude / 33 + 20, the amplitude being the
 *   one set on the instrument), so the output stays inside the range stock itself uses. The
 *   host gives a position inside the table, never an address.
 *
 * What it writes
 *   - its own SRAM block at LINK_RAM (above stock's stack top, below the IAP RAM magic);
 *   - the two core pointers named above, and the endpoint bookkeeping fields the stock class
 *     itself writes when it arms an endpoint;
 *   - USB device registers (connect/disconnect, endpoint status);
 *   - on a WAVE request, the 200 bytes of the generator's table (state[0xE62..0xF29]).
 *   Nothing else in stock's SRAM, and never the flash.
 *
 * No .data/.bss, no libc, integer registers only (the linker script and -mgeneral-regs-only
 * enforce it). Every address below is a V1.2.0 fact, verified against the binary whose
 * sha256 the builder pins.
 */
#include <stdint.h>

/* ---- stock V1.2.0 facts ------------------------------------------------------------------ */

#define UDEV                 ((uint8_t *)0x20002B24u)      /* usbd_core_type                  */
#define UDEV_CLASS           (*(volatile uint32_t *)0x20002B28u)
#define UDEV_DESC            (*(volatile uint32_t *)0x20002B2Cu)
#define STOCK_CLASS_TABLE    0x20000030u                   /* stock MSC usbd_class_handler    */
#define STOCK_DESC_TABLE     0x20000058u                   /* stock usbd_desc_handler         */

#define STATE_BASE           0x200000F8u                   /* stock's big state structure     */
#define SYSTEM_MODE          (*(volatile uint8_t *)0x20001060u)   /* state[0xF68]              */
#define X_TICK_COUNT         (*(volatile uint32_t *)0x200062D4u)  /* FreeRTOS xTickCount       */

#define METER_RX_INDEX       (*(volatile uint8_t *)0x20004E10u)   /* USART2 ISR write index    */
#define METER_RX_BUF         ((const volatile uint8_t *)0x20004E11u)   /* 12-byte frame        */
#define METER_TX_FUNCTION    (*(volatile uint8_t *)0x20000008u)   /* last command word sent    */
#define METER_STATE          ((const volatile uint8_t *)0x20001020u)   /* state[0xF28..]       */
#define METER_FRAME_LEN      12u
#define METER_STATE_LEN      40u

#define SCOPE_CH1_BUF        ((const volatile uint8_t *)0x200006A8u)   /* state[0x5B0]         */
#define SCOPE_CH2_BUF        ((const volatile uint8_t *)0x20000AA8u)   /* state[0x9B0]         */
#define SCOPE_RECORD_LEN     1024u
#define SCOPE_HDR_LEN        56u                           /* state[0x00..0x37]               */
#define SCOPE_RUN_FLAG       (*(volatile uint8_t *)0x20000126u)   /* state[0x2E]: 1 run, 0 stop */
#define SCOPE_MEASURED       ((const volatile uint8_t *)0x20000208u)  /* shown: CH1 then CH2     */
#define SCOPE_MEASURED_LEN   48u                           /* twelve words per channel        */
#define GENERATOR_STATE      ((const volatile uint8_t *)0x20000F50u)  /* state[0xE58]            */
#define GENERATOR_STATE_LEN  10u                           /* wave, frequency, duty, amplitude */
#define GENERATOR_WAVEFORM   (*(volatile uint8_t *)0x20000F51u)   /* state[0xE59]: 0..12       */
#define GENERATOR_AMPLITUDE  (*(volatile uint8_t *)0x20000F59u)   /* state[0xE61]: tenths of V */
#define GENERATOR_TABLE      ((volatile uint16_t *)0x20000F5Au)   /* state[0xE62]: what the DMA plays */
#define GENERATOR_POINTS     100u
#define GENERATOR_SAMPLE_MAX 4094u                         /* top of stock's own sine table    */
#define GENERATOR_AMPL_MAX   30u                           /* stock's menu stops at 3.0 V      */
#define GENERATOR_DIVISOR    33u                           /* stock: sample * amplitude / 33   */
#define GENERATOR_FLOOR      20u                           /* ... + 20                         */
#define FPGA_OP_READ_CH1     4u
#define FPGA_OP_READ_CH2     5u

#define USB_EPR(n)           (*(volatile uint32_t *)(0x40005C00u + 4u * (n)))
#define USB_CTRL             (*(volatile uint32_t *)0x40005C40u)
#define USB_INTSTS           (*(volatile uint32_t *)0x40005C44u)
#define USB_CFG              (*(volatile uint32_t *)0x40005C60u)
#define USB_CTRL_DISUSB      0x2u
#define USB_CFG_PUO          0x2u

/* stock functions (Thumb) */
typedef void (*ept_open_fn)(void *udev, uint32_t ept_addr, uint32_t ept_type);
typedef void (*ept_close_fn)(void *udev, uint32_t ept_addr);
typedef void (*ept_send_fn)(void *udev, uint32_t ept_addr, const uint8_t *buf, uint32_t len);
#define stock_ept_open       ((ept_open_fn)0x08040B25u)    /* maxpacket fixed at 64           */
#define stock_ept_close      ((ept_close_fn)0x08040875u)
#define stock_ept_send       ((ept_send_fn)0x08040EB5u)
#define STOCK_SERIAL_STRING  0x08036DB5u                   /* desc handler: serial from UID   */
#define EPT_TYPE_INT         2u                            /* this build: 0 ctl 1 bulk 2 int  */

/* usbd_core_type offsets (ept_in[] at 0x0C, ept_out[] at 0x10C, 0x20 bytes per endpoint) */
#define U8(off)              (*(volatile uint8_t *)(UDEV + (off)))
#define U16(off)             (*(volatile uint16_t *)(UDEV + (off)))
#define U32(off)             (*(volatile uint32_t *)(UDEV + (off)))
#define IN0_EPTN             0x00Cu
#define IN0_STALL            0x017u
#define IN0_REM0_LEN         0x026u
#define IN0_EPT0_SLEN        0x028u
#define OUT0_EPTN            0x10Cu
#define OUT0_STALL           0x117u
#define OUT1_EPTN            0x12Cu
#define OUT1_TOTAL_LEN       0x13Au
#define OUT1_TRANS_LEN       0x13Cu
#define OUT1_TRANS_BUF       0x140u
#define EPT0_STS             0x220u
#define EPT0_STS_DATA_IN     2u

/* ---- link parameters --------------------------------------------------------------------- */

#define LINK_RAM             0x20037000u
#define LINK_RAM_LIMIT       0x20037FE0u                   /* factory IAP keeps a magic here   */
#define LINK_MAGIC           0x4B4E4C33u                   /* "3LNK"                           */
#define LINK_PROTO_VERSION   1u                            /* v1 requests and events unchanged */
#define LINK_PATCH_VERSION   4u
#define LINK_FEATURES        0x03u                         /* INFO: bit 0 = the status stream,
                                                              bit 1 = the WAVE request         */
#define LINK_BOOT_HOLDOFF    3000u                         /* ticks after boot: hands off      */
#define LINK_SETTLE_TICKS    1000u                         /* bus must stay idle this long     */

#define EP_IN                0x81u
#define EP_OUT               0x01u
#define REPORT_LEN           64u
#define PAYLOAD_MAX          56u

#define CMD_INFO             0x01u
#define CMD_READ             0x02u
#define CMD_STREAM           0x03u
#define CMD_WAVE             0x04u
#define WAVE_MAX_SAMPLES     (PAYLOAD_MAX / 2u)            /* 28 samples of 16 bits a request  */
#define EVT_METER            0x90u
#define EVT_SCOPE_HEADER     0xA0u
#define EVT_SCOPE_DATA       0xA1u
#define EVT_STATUS           0xB0u
#define ST_OK                0u
#define ST_BAD_CMD           1u
#define ST_BAD_RANGE         2u
#define ST_BAD_LEN           3u

#define STREAM_METER         0x01u
#define STREAM_SCOPE         0x02u
#define STREAM_STATUS        0x04u
#define STREAM_ALL           (STREAM_METER | STREAM_SCOPE | STREAM_STATUS)
#define STATUS_PERIOD_TICKS  100u                          /* one status block every 100 ms    */
#define STATUS_GENERATOR     1u                            /* 10 bytes of GENERATOR_STATE      */
#define STATUS_SCOPE_CH1     2u                            /* 48 bytes of measurements + run   */
#define STATUS_SCOPE_CH2     3u
#define STATUS_METER         4u                            /* meter state bytes 40..55         */
#define STATUS_KINDS         4u
#define STATUS_METER_OFFSET  40u
#define STATUS_METER_LEN     16u
#define MODE_METER           1u
#define MODE_SCOPE           2u
#define MODE_GENERATOR       3u
#define METER_KIND_DATA      1u                            /* 5A A5 ..., 12 bytes              */
#define METER_KIND_ECHO      2u                            /* AA 55 ..., 10 bytes              */
#define TX_NONE              0u
#define TX_RESPONSE          1u
#define TX_EVENT             2u
#define NO_CHANNEL           0xFFu
#define OFFSET_HEADER        0xFFFFu                       /* record header not sent yet       */

#ifndef LINK_USB_VID
#define LINK_USB_VID         0x1209u                       /* pid.codes                        */
#endif
#ifndef LINK_USB_PID
#define LINK_USB_PID         0x0001u                       /* pid.codes test PID (provisional) */
#endif

/* One oscilloscope record, shared between the `fpga` task (writer) and the USB interrupt
 * (reader). The task cannot preempt the interrupt, so the handshake only has to survive the
 * interrupt arriving anywhere inside the writer:
 *   writer:  valid = 0;  if (sending) { valid = 1; leave it alone }  else copy, seq++, valid = 1
 *   reader:  starts a transfer only if valid, then holds `sending` until the last chunk. */
typedef struct {
    volatile uint8_t  valid;
    volatile uint8_t  sending;
    volatile uint16_t seq;
    uint16_t sent_seq;
    uint16_t rsvd;
    uint32_t tick;
    uint8_t  hdr[SCOPE_HDR_LEN];
    uint8_t  data[SCOPE_RECORD_LEN];
} scope_record_t;

typedef struct {
    uint32_t magic;
    uint16_t settle;            /* ticks the bus has been idle in a measurement mode */
    uint8_t  tx_busy;
    uint8_t  tx_kind;           /* what the report in flight is */
    uint32_t n_links;           /* times the link was connected since power-on */
    uint32_t n_requests;
    uint32_t n_meter;           /* meter frames seen */
    uint32_t n_scope;           /* scope records captured */
    uint32_t n_scope_skipped;   /* records not captured because one was being sent */
    uint8_t  ctl[4];            /* one-byte control replies */
    volatile uint8_t stream;    /* STREAM_* subscription mask */
    uint8_t  resp_pending;
    uint8_t  meter_prev_index;
    uint8_t  meter_pending;     /* METER_KIND_* of the frame waiting to go out */
    uint8_t  meter_seq;
    uint8_t  cur_ch;            /* scope record being sent, or NO_CHANNEL */
    uint16_t cur_off;
    uint8_t  next_ch;
    uint8_t  status_pending;    /* STATUS_* waiting to go out, or 0 */
    uint8_t  status_last;       /* STATUS_* queued last: the round starts after it */
    uint8_t  status_seq;
    uint32_t status_due;        /* tick at which the next status block is queued */
    uint32_t meter_tick;
    uint8_t  meter_frame[METER_FRAME_LEN];
    uint8_t  rx[REPORT_LEN];
    uint8_t  tx[REPORT_LEN];
    uint8_t  resp[REPORT_LEN];
    scope_record_t rec[2];
} link_state_t;

_Static_assert(sizeof(link_state_t) % 4u == 0u, "state is cleared word by word");
_Static_assert(LINK_RAM + sizeof(link_state_t) <= LINK_RAM_LIMIT, "state must end below the IAP RAM magic");

#define S ((link_state_t *)LINK_RAM)

/* ---- descriptors ------------------------------------------------------------------------- */

typedef struct {
    uint16_t length;
    const uint8_t *descriptor;
} usbd_desc_t;

#define DESC_ALIGN __attribute__((aligned(4)))    /* the core copies with 16-bit loads */

static const uint8_t report_desc[] DESC_ALIGN = {
    0x06, 0x00, 0xFF,       /* Usage Page (Vendor 0xFF00) */
    0x09, 0x01,             /* Usage (1) */
    0xA1, 0x01,             /* Collection (Application) */
    0x15, 0x00,             /*   Logical Minimum (0) */
    0x26, 0xFF, 0x00,       /*   Logical Maximum (255) */
    0x75, 0x08,             /*   Report Size (8) */
    0x95, REPORT_LEN,       /*   Report Count (64) */
    0x09, 0x01,             /*   Usage (1) */
    0x81, 0x02,             /*   Input (Data,Var,Abs) */
    0x95, REPORT_LEN,       /*   Report Count (64) */
    0x09, 0x01,             /*   Usage (1) */
    0x91, 0x02,             /*   Output (Data,Var,Abs) */
    0xC0                    /* End Collection */
};

static const uint8_t device_desc[18] DESC_ALIGN = {
    18, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 64,
    (uint8_t)LINK_USB_VID, (uint8_t)(LINK_USB_VID >> 8),
    (uint8_t)LINK_USB_PID, (uint8_t)(LINK_USB_PID >> 8),
    (uint8_t)LINK_PATCH_VERSION, 0x00,      /* bcdDevice */
    1, 2, 3, 1
};

#define CONFIG_TOTAL 41
#define HID_DESC_OFFSET 18
static const uint8_t config_desc[CONFIG_TOTAL] DESC_ALIGN = {
    9, 0x02, CONFIG_TOTAL, 0, 1, 1, 0, 0xC0, 0x32,
    9, 0x04, 0, 0, 2, 0x03, 0x00, 0x00, 0,                  /* interface: HID, no boot */
    9, 0x21, 0x11, 0x01, 0x00, 1, 0x22, sizeof(report_desc), 0,
    7, 0x05, EP_IN, 0x03, REPORT_LEN, 0, 1,                 /* interrupt IN, 1 ms */
    7, 0x05, EP_OUT, 0x03, REPORT_LEN, 0, 1                 /* interrupt OUT, 1 ms */
};

static const uint8_t lang_desc[4] DESC_ALIGN = { 4, 0x03, 0x09, 0x04 };

#define USTR(name, ...) static const uint8_t name[] DESC_ALIGN = { 2 + sizeof((uint8_t[]){__VA_ARGS__}), 0x03, __VA_ARGS__ }
#define C(c) (c), 0
USTR(manufacturer_desc, C('F'), C('N'), C('I'), C('R'), C('S'), C('I'));
USTR(product_desc, C('2'), C('C'), C('5'), C('3'), C('T'), C(' '), C('s'), C('t'), C('o'), C('c'), C('k'),
     C('-'), C('l'), C('i'), C('n'), C('k'));

static const usbd_desc_t d_device = { sizeof(device_desc), device_desc };
static const usbd_desc_t d_config = { sizeof(config_desc), config_desc };
static const usbd_desc_t d_lang = { sizeof(lang_desc), lang_desc };
static const usbd_desc_t d_manufacturer = { sizeof(manufacturer_desc), manufacturer_desc };
static const usbd_desc_t d_product = { sizeof(product_desc), product_desc };

static const usbd_desc_t *get_device(void) { return &d_device; }
static const usbd_desc_t *get_config(void) { return &d_config; }
static const usbd_desc_t *get_lang(void) { return &d_lang; }
static const usbd_desc_t *get_manufacturer(void) { return &d_manufacturer; }
static const usbd_desc_t *get_product(void) { return &d_product; }
static const usbd_desc_t *get_none(void) { return 0; }

/* Same slot order as stock's table at 0x20000058. The serial slot reuses stock's own function
 * so the link reports the same serial number as the mass-storage drive. */
static const uint32_t link_desc_table[10] = {
    (uint32_t)get_device,           /* device */
    (uint32_t)get_none,             /* device qualifier */
    (uint32_t)get_config,           /* configuration */
    (uint32_t)get_none,             /* other speed */
    (uint32_t)get_lang,             /* lang id */
    (uint32_t)get_manufacturer,     /* manufacturer string */
    (uint32_t)get_product,          /* product string */
    STOCK_SERIAL_STRING,            /* serial string */
    (uint32_t)get_product,          /* interface string */
    (uint32_t)get_product           /* configuration string */
};

static const uint32_t link_class_table[10];

/* ---- small helpers ----------------------------------------------------------------------- */

static uint32_t irq_save(void) {
    uint32_t m;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(m));
    return m;
}

static void irq_restore(uint32_t m) {
    __asm volatile("msr primask, %0" : : "r"(m));
}

static void state_check(void) {
    if (S->magic != LINK_MAGIC) {
        volatile uint32_t *p = (volatile uint32_t *)LINK_RAM;
        uint32_t i;
        for (i = 0; i < sizeof(link_state_t) / 4u; ++i) {
            p[i] = 0u;
        }
        S->cur_ch = NO_CHANNEL;
        S->magic = LINK_MAGIC;
    }
}

static void put32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t get32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void copy_bytes(uint8_t *dst, const volatile uint8_t *src, uint32_t n) {
    uint32_t i;
    for (i = 0; i < n; ++i) {
        dst[i] = src[i];
    }
}

static void clear_report(uint8_t *p) {
    uint32_t i;
    for (i = 0; i < REPORT_LEN; ++i) {
        p[i] = 0u;
    }
}

/* Set the subscription and forget everything captured so far, sent or not: only what is
 * measured after this point goes out. */
static void stream_reset(uint32_t mask) {
    S->stream = (uint8_t)mask;
    S->meter_pending = 0u;
    S->status_pending = 0u;
    S->status_last = 0u;
    S->status_due = X_TICK_COUNT;
    S->cur_ch = NO_CHANNEL;
    S->rec[0].sending = 0u;
    S->rec[1].sending = 0u;
    S->rec[0].sent_seq = S->rec[0].seq;
    S->rec[1].sent_seq = S->rec[1].seq;
}

/* STAT_RX <- VALID on the OUT endpoint, the way the stock class arms its own (0x08030DEA). */
static void arm_rx(void) {
    uint32_t n = U8(OUT1_EPTN);
    U32(OUT1_TRANS_BUF) = (uint32_t)S->rx;
    U16(OUT1_TOTAL_LEN) = 0u;
    U16(OUT1_TRANS_LEN) = 0u;
    USB_EPR(n) = (USB_EPR(n) & 0x3F0Fu) ^ 0xB080u;
}

/* usbd_ctrl_send(), inlined in stock (0x0803109C): data stage on endpoint 0. */
static void ctrl_send(const uint8_t *buf, uint32_t len, uint32_t wlength) {
    if (len > wlength) {
        len = wlength;
    }
    U16(IN0_REM0_LEN) = (uint16_t)len;
    U16(IN0_EPT0_SLEN) = (uint16_t)len;
    U8(EPT0_STS) = EPT0_STS_DATA_IN;
    stock_ept_send(UDEV, 0u, buf, len);
}

/* usbd_ctrl_unsupport(), inlined in stock (0x08031126): STALL both directions of endpoint 0. */
static void ctrl_stall(void) {
    uint32_t n = U8(OUT0_EPTN);
    USB_EPR(n) = (USB_EPR(n) & 0x3F0Fu) ^ 0x9080u;
    U8(OUT0_STALL) = 1u;
    n = U8(IN0_EPTN);
    USB_EPR(n) = (USB_EPR(n) & 0x0F3Fu) ^ 0x8090u;
    U8(IN0_STALL) = 1u;
}

/* ---- requests ---------------------------------------------------------------------------- */

static uint32_t readable(uint32_t addr, uint32_t len) {
    uint32_t end = addr + len;
    if (end < addr) {
        return 0u;
    }
    if (addr >= 0x08000000u && end <= 0x08100000u) {
        return 1u;                                  /* MCU flash, 1 MB */
    }
    if (addr >= 0x20000000u && end <= 0x20038000u) {
        return 1u;                                  /* SRAM, 224 KB */
    }
    return 0u;
}

static void handle_request(uint32_t rx_len) {
    const uint8_t *rq = S->rx;
    uint8_t *rs = S->resp;
    uint32_t i, n = 0u, status = ST_OK;
    uint32_t addr = get32(rq + 4);

    clear_report(rs);
    rs[0] = (uint8_t)(rq[0] | 0x80u);
    rs[1] = rq[1];
    put32(rs + 4, addr);

    if (rx_len < 8u) {
        status = ST_BAD_LEN;
    } else if (rq[0] == CMD_INFO) {
        static const uint8_t tag[8] = { '2', 'C', '5', '3', 'L', 'I', 'N', 'K' };
        for (i = 0; i < 8u; ++i) {
            rs[8u + i] = tag[i];
        }
        rs[16] = LINK_PROTO_VERSION;
        rs[17] = LINK_PATCH_VERSION;
        rs[18] = SYSTEM_MODE;
        rs[19] = S->stream;
        put32(rs + 20, X_TICK_COUNT);
        put32(rs + 24, S->n_links);
        put32(rs + 28, S->n_requests);
        put32(rs + 32, S->n_meter);
        put32(rs + 36, S->n_scope);
        put32(rs + 40, S->n_scope_skipped);
        rs[44] = LINK_FEATURES;
        n = 40u;
    } else if (rq[0] == CMD_READ) {
        n = rq[2];
        if (n == 0u || n > PAYLOAD_MAX) {
            status = ST_BAD_LEN;
            n = 0u;
        } else if (!readable(addr, n)) {
            status = ST_BAD_RANGE;
            n = 0u;
        } else {
            copy_bytes(rs + 8, (const volatile uint8_t *)addr, n);
        }
    } else if (rq[0] == CMD_STREAM) {
        if (rq[2] & ~STREAM_ALL) {
            status = ST_BAD_LEN;
        } else {
            stream_reset(rq[2]);
            rs[8] = S->stream;
            n = 1u;
        }
    } else if (rq[0] == CMD_WAVE) {
        /* rq[2] samples of 16 bits from rq[8], for positions addr.. of the table. Both are
         * checked against the table's 100 points before anything is stored, and every value
         * is brought into stock's own range: the request cannot reach another byte of SRAM
         * nor make the DAC leave the span stock drives it over. */
        uint32_t count = rq[2];
        if (count == 0u || count > WAVE_MAX_SAMPLES || rx_len < 8u + 2u * count) {
            status = ST_BAD_LEN;
        } else if (addr >= GENERATOR_POINTS || count > GENERATOR_POINTS - addr) {
            status = ST_BAD_RANGE;
        } else {
            uint32_t ampl = GENERATOR_AMPLITUDE;
            if (ampl > GENERATOR_AMPL_MAX) {
                ampl = GENERATOR_AMPL_MAX;
            }
            for (i = 0; i < count; ++i) {
                uint32_t v = (uint32_t)rq[8u + 2u * i] | ((uint32_t)rq[9u + 2u * i] << 8);
                if (v > GENERATOR_SAMPLE_MAX) {
                    v = GENERATOR_SAMPLE_MAX;
                }
                GENERATOR_TABLE[addr + i] = (uint16_t)(v * ampl / GENERATOR_DIVISOR + GENERATOR_FLOOR);
            }
            rs[8] = (uint8_t)ampl;                  /* the amplitude the samples were scaled by */
            rs[9] = GENERATOR_WAVEFORM;             /* the waveform stock will go back to       */
            n = 2u;
        }
    } else {
        status = ST_BAD_CMD;
    }
    rs[2] = (uint8_t)status;
    rs[3] = (uint8_t)n;
    S->n_requests = S->n_requests + 1u;
}

/* ---- events ------------------------------------------------------------------------------ */

/* Called every millisecond. Bytes arrive every 1.04 ms, so the index is seen at (nearly)
 * every value; a wrap back towards zero with the right header in place means the interrupt
 * handler has just stored the last byte of a frame. A 5A A5 frame only ever restarts at 12
 * bytes, an AA 55 echo at 10. */
static void meter_poll(void) {
    uint32_t idx = METER_RX_INDEX;
    uint32_t prev = S->meter_prev_index;
    uint32_t kind = 0u;

    S->meter_prev_index = (uint8_t)idx;
    if (idx >= prev) {
        return;
    }
    if (prev >= 10u && METER_RX_BUF[0] == 0x5Au && METER_RX_BUF[1] == 0xA5u) {
        kind = METER_KIND_DATA;
    } else if (prev >= 8u && METER_RX_BUF[0] == 0xAAu && METER_RX_BUF[1] == 0x55u) {
        kind = METER_KIND_ECHO;
    }
    if (kind == 0u) {
        return;
    }
    S->n_meter = S->n_meter + 1u;
    if (S->stream & STREAM_METER) {
        copy_bytes(S->meter_frame, METER_RX_BUF, METER_FRAME_LEN);
        S->meter_tick = X_TICK_COUNT;
        S->meter_seq = (uint8_t)(S->meter_seq + 1u);
        S->meter_pending = (uint8_t)kind;          /* a newer frame replaces an unsent one */
    }
}

/* Which status blocks mean something in which mode. Stock only keeps a block up to date in
 * its own mode: the scope's measurements are zeros elsewhere, the meter's state is stale. */
static uint32_t status_applies(uint32_t kind, uint32_t mode) {
    if (kind == STATUS_GENERATOR) {
        return mode == MODE_SCOPE || mode == MODE_GENERATOR;
    }
    if (kind == STATUS_METER) {
        return mode == MODE_METER;
    }
    return mode == MODE_SCOPE;                      /* STATUS_SCOPE_CH1, STATUS_SCOPE_CH2 */
}

/* Called every millisecond. When the period is up and the last status block has gone out,
 * queues the next kind that applies, round robin. The data is copied when the report is built. */
static void status_poll(void) {
    uint32_t now = X_TICK_COUNT;
    uint32_t mode = SYSTEM_MODE;
    uint32_t i, kind;

    if (!(S->stream & STREAM_STATUS) || S->status_pending) {
        return;
    }
    if ((uint32_t)(now - S->status_due) >= 0x80000000u) {
        return;                                     /* not due yet (wrap-safe) */
    }
    S->status_due = now + STATUS_PERIOD_TICKS;
    kind = S->status_last;
    for (i = 0; i < STATUS_KINDS; ++i) {
        kind = (kind >= STATUS_KINDS) ? 1u : kind + 1u;
        if (status_applies(kind, mode)) {
            S->status_last = (uint8_t)kind;
            S->status_pending = (uint8_t)kind;
            return;
        }
    }
}

static void build_status_event(uint8_t *rs) {
    uint32_t kind = S->status_pending;
    uint32_t n;

    clear_report(rs);
    S->status_seq = (uint8_t)(S->status_seq + 1u);
    rs[0] = EVT_STATUS;
    rs[1] = S->status_seq;
    rs[2] = (uint8_t)kind;
    put32(rs + 4, X_TICK_COUNT);
    if (kind == STATUS_GENERATOR) {
        n = GENERATOR_STATE_LEN;
        copy_bytes(rs + 8, GENERATOR_STATE, n);
    } else if (kind == STATUS_METER) {
        n = STATUS_METER_LEN;
        copy_bytes(rs + 8, METER_STATE + STATUS_METER_OFFSET, n);
    } else {
        n = SCOPE_MEASURED_LEN;
        copy_bytes(rs + 8, SCOPE_MEASURED + (kind == STATUS_SCOPE_CH2 ? SCOPE_MEASURED_LEN : 0u), n);
        rs[8u + n] = SCOPE_RUN_FLAG;
        n += 1u;
    }
    rs[3] = (uint8_t)n;
}

static void build_meter_event(uint8_t *rs) {
    clear_report(rs);
    rs[0] = EVT_METER;
    rs[1] = S->meter_seq;
    rs[2] = S->meter_pending;
    rs[3] = (uint8_t)PAYLOAD_MAX;
    put32(rs + 4, S->meter_tick);
    copy_bytes(rs + 8, S->meter_frame, METER_FRAME_LEN);
    rs[20] = METER_TX_FUNCTION;
    rs[21] = SYSTEM_MODE;
    copy_bytes(rs + 24, METER_STATE, METER_STATE_LEN);
}

/* Next report of the scope stream into rs; 0 if there is nothing to send. */
static uint32_t build_scope_event(uint8_t *rs) {
    scope_record_t *r;
    uint32_t i, ch, off, n;

    if (S->cur_ch == NO_CHANNEL) {
        for (i = 0; i < 2u; ++i) {
            ch = (S->next_ch + i) & 1u;
            r = &S->rec[ch];
            if (r->valid && r->seq != r->sent_seq) {
                r->sending = 1u;
                S->cur_ch = (uint8_t)ch;
                S->cur_off = OFFSET_HEADER;
                S->next_ch = (uint8_t)(ch ^ 1u);
                break;
            }
        }
        if (S->cur_ch == NO_CHANNEL) {
            return 0u;
        }
    }
    r = &S->rec[S->cur_ch];
    clear_report(rs);
    rs[1] = (uint8_t)r->seq;
    rs[2] = S->cur_ch;
    if (S->cur_off == OFFSET_HEADER) {
        rs[0] = EVT_SCOPE_HEADER;
        rs[3] = (uint8_t)SCOPE_HDR_LEN;
        put32(rs + 4, r->tick);
        copy_bytes(rs + 8, r->hdr, SCOPE_HDR_LEN);
        S->cur_off = 0u;
        return 1u;
    }
    off = S->cur_off;
    n = SCOPE_RECORD_LEN - off;
    if (n > PAYLOAD_MAX) {
        n = PAYLOAD_MAX;
    }
    rs[0] = EVT_SCOPE_DATA;
    rs[3] = (uint8_t)n;
    put32(rs + 4, off);
    copy_bytes(rs + 8, r->data + off, n);
    off += n;
    if (off >= SCOPE_RECORD_LEN) {
        r->sent_seq = r->seq;
        r->sending = 0u;
        S->cur_ch = NO_CHANNEL;
    } else {
        S->cur_off = (uint16_t)off;
    }
    return 1u;
}

/* Send the next report if the IN endpoint is free: responses first, then the meter and the
 * status blocks (small and rare), then scope chunks. */
static void pump(void *udev) {
    const uint8_t *buf = S->tx;
    uint32_t kind = TX_EVENT;

    if (S->tx_busy) {
        return;
    }
    if (S->resp_pending) {
        S->resp_pending = 0u;
        buf = S->resp;
        kind = TX_RESPONSE;
    } else if ((S->stream & STREAM_METER) && S->meter_pending) {
        build_meter_event(S->tx);
        S->meter_pending = 0u;
    } else if ((S->stream & STREAM_STATUS) && S->status_pending) {
        build_status_event(S->tx);
        S->status_pending = 0u;
    } else if (!(S->stream & STREAM_SCOPE) || !build_scope_event(S->tx)) {
        return;
    }
    S->tx_busy = 1u;
    S->tx_kind = (uint8_t)kind;
    stock_ept_send(udev, EP_IN, buf, REPORT_LEN);
}

/* ---- usbd class handler ------------------------------------------------------------------ */

static uint32_t link_init(void *udev) {
    state_check();
    stock_ept_open(udev, EP_IN, EPT_TYPE_INT);
    stock_ept_open(udev, EP_OUT, EPT_TYPE_INT);
    S->tx_busy = 0u;
    S->tx_kind = TX_NONE;
    S->resp_pending = 0u;
    S->meter_prev_index = 0u;
    stream_reset(0u);                               /* every session starts unsubscribed */
    arm_rx();
    return 0u;
}

static uint32_t link_clear(void *udev) {
    stock_ept_close(udev, EP_IN);
    stock_ept_close(udev, EP_OUT);
    return 0u;
}

static uint32_t link_setup(void *udev, const uint8_t *setup) {
    uint32_t type = setup[0] & 0x60u;
    uint32_t request = setup[1];
    uint32_t desc_type = setup[3];
    uint32_t wlength = (uint32_t)setup[6] | ((uint32_t)setup[7] << 8);
    (void)udev;
    state_check();

    if (type == 0x00u) {                            /* standard, interface recipient */
        if (request == 0x06u) {                     /* GET_DESCRIPTOR */
            if (desc_type == 0x22u) {
                ctrl_send(report_desc, sizeof(report_desc), wlength);
            } else if (desc_type == 0x21u) {
                ctrl_send(config_desc + HID_DESC_OFFSET, 9u, wlength);
            } else {
                ctrl_stall();
            }
        } else if (request == 0x0Au) {              /* GET_INTERFACE */
            S->ctl[0] = 0u;
            ctrl_send(S->ctl, 1u, wlength);
        } else if (request != 0x0Bu) {              /* SET_INTERFACE needs nothing */
            ctrl_stall();
        }
    } else if (type == 0x20u) {                     /* HID class */
        if (request == 0x02u) {                     /* GET_IDLE */
            S->ctl[0] = 0u;
            ctrl_send(S->ctl, 1u, wlength);
        } else if (request == 0x03u) {              /* GET_PROTOCOL: report protocol */
            S->ctl[0] = 1u;
            ctrl_send(S->ctl, 1u, wlength);
        } else if (request != 0x0Au && request != 0x0Bu) {
            ctrl_stall();                           /* SET_IDLE / SET_PROTOCOL accepted */
        }
    } else {
        ctrl_stall();
    }
    return 0u;
}

static uint32_t link_ept0(void *udev) {
    (void)udev;
    return 0u;
}

/* A report has gone out. After a response the host may send the next request. */
static uint32_t link_in(void *udev, uint32_t ept_num) {
    (void)ept_num;
    S->tx_busy = 0u;
    if (S->tx_kind == TX_RESPONSE) {
        arm_rx();
    }
    S->tx_kind = TX_NONE;
    pump(udev);
    return 0u;
}

/* Request received. The OUT endpoint stays NAK until its response has gone out, so requests
 * and responses pair up strictly; events may be interleaved before the response. */
static uint32_t link_out(void *udev, uint32_t ept_num) {
    (void)ept_num;
    handle_request(U16(OUT1_TRANS_LEN));
    S->resp_pending = 1u;
    pump(udev);
    return 0u;
}

/* Start of frame: once per millisecond while a host is attached. */
static uint32_t link_sof(void *udev) {
    if (S->magic != LINK_MAGIC) {
        return 0u;
    }
    meter_poll();
    status_poll();
    pump(udev);
    return 0u;
}

/* usbd_class_handler, same layout as stock's table at 0x20000030. */
static const uint32_t link_class_table[10] = {
    (uint32_t)link_init,
    (uint32_t)link_clear,
    (uint32_t)link_setup,
    (uint32_t)link_ept0,            /* ept0 tx */
    (uint32_t)link_ept0,            /* ept0 rx */
    (uint32_t)link_in,
    (uint32_t)link_out,
    (uint32_t)link_sof,
    0u,                             /* event: unused (the core checks for null) */
    LINK_RAM
};

/* ---- scope capture: called from the `fpga` task after each SPI3 command ------------------ */

void link_fpga_tail(uint32_t opcode) {
    scope_record_t *r;
    const volatile uint8_t *src;

    if (S->magic != LINK_MAGIC || !(S->stream & STREAM_SCOPE)) {
        return;                                     /* the usual case: two loads and out */
    }
    if (opcode == FPGA_OP_READ_CH1) {
        r = &S->rec[0];
        src = SCOPE_CH1_BUF;
    } else if (opcode == FPGA_OP_READ_CH2) {
        r = &S->rec[1];
        src = SCOPE_CH2_BUF;
    } else {
        return;
    }
    r->valid = 0u;
    if (r->sending) {
        r->valid = 1u;                              /* still going out: keep it intact */
        S->n_scope_skipped = S->n_scope_skipped + 1u;
        return;
    }
    copy_bytes(r->hdr, (const volatile uint8_t *)STATE_BASE, SCOPE_HDR_LEN);
    copy_bytes(r->data, src, SCOPE_RECORD_LEN);
    r->tick = X_TICK_COUNT;
    r->seq = (uint16_t)(r->seq + 1u);
    S->n_scope = S->n_scope + 1u;
    r->valid = 1u;
}

/* ---- supervisor: called from the SysTick hook, every tick -------------------------------- */

static uint32_t usb_connected(void) {
    return !(USB_CTRL & USB_CTRL_DISUSB) && !(USB_CFG & USB_CFG_PUO);
}

static void usb_disconnect(void) {                  /* same two writes as stock (0x0800E336) */
    USB_CTRL = USB_CTRL | USB_CTRL_DISUSB;
    USB_CFG = USB_CFG | USB_CFG_PUO;
}

static void usb_connect(void) {                     /* same two writes as stock (0x080106B6) */
    USB_CTRL = USB_CTRL & ~USB_CTRL_DISUSB;
    USB_CFG = USB_CFG & ~USB_CFG_PUO;
}

void link_tick(void) {
    uint32_t mode = SYSTEM_MODE;
    uint32_t ours = (UDEV_CLASS == (uint32_t)link_class_table);
    uint32_t m;

    state_check();

    if (mode < 1u || mode > 3u) {
        /* Not measuring: USB belongs to stock. Hand it back within one tick, well before
         * stock's own connect in USB Sharing (which first decodes a full-screen JPEG). */
        S->settle = 0u;
        if (ours) {
            m = irq_save();
            usb_disconnect();
            /* Drop anything still pending from the link session, so stock's class never
             * sees a transfer that was addressed to ours: writing 0 clears the two
             * transfer-complete bits of an endpoint register and leaves the rest alone. */
            USB_EPR(0) = USB_EPR(0) & 0x0F0Fu;
            USB_EPR(1) = USB_EPR(1) & 0x0F0Fu;
            USB_INTSTS = 0u;
            UDEV_CLASS = STOCK_CLASS_TABLE;
            UDEV_DESC = STOCK_DESC_TABLE;
            stream_reset(0u);                       /* and stop copying scope records */
            irq_restore(m);
        }
        return;
    }

    if (X_TICK_COUNT < LINK_BOOT_HOLDOFF) {
        S->settle = 0u;
        return;
    }

    if (usb_connected()) {
        /* Either our link is up, or stock is using the bus: nothing to do either way. */
        S->settle = 0u;
        return;
    }

    /* Measurement mode and the bus is disconnected. Stock disconnects on every mode change
     * and before it rewrites its settings page, so wait for the bus to stay idle before
     * taking it (again). Nobody is listening meanwhile. */
    if (S->stream) {
        m = irq_save();
        stream_reset(0u);
        irq_restore(m);
    }
    if (S->settle < LINK_SETTLE_TICKS) {
        S->settle = (uint16_t)(S->settle + 1u);
        return;
    }
    S->settle = 0u;

    m = irq_save();
    if (!usb_connected()) {
        UDEV_CLASS = (uint32_t)link_class_table;
        UDEV_DESC = (uint32_t)link_desc_table;
        S->tx_busy = 0u;
        S->n_links = S->n_links + 1u;
        usb_connect();
    }
    irq_restore(m);
}
