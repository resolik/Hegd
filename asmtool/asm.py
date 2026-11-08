"""asm v0.1 - run AArch64 (ARM64) assembly scripts in a safe virtual processor, inside Butter.

Usage (inside Butter):
    py asm.py prog.s                 run the program
    py asm.py prog.s --trace         print every executed instruction
    py asm.py prog.s --regs          print the registers when the program ends
    py asm.py prog.s --steps 500000  stop after this many instructions (default 1000000)
    py asm.py prog.s --input "text"  what the read system call gets (the app has no keyboard input for scripts)

The program is NOT run on the real processor: it is executed by this emulator, so a bug in your
assembly can crash only the script, never the app. It only sees its own memory (data and stack).

What is supported (GNU syntax, comments with // or ;):
  sections    .text .data .bss .rodata       directives  .global .equ .set .asciz .string .ascii
              .byte .hword .word .quad .space .zero .align
  registers   x0-x30 w0-w30 sp xzr wzr lr fp, flags N Z C V
  move        mov movz movk movn mvn adr adrp  ldr xN, =label
  arithmetic  add sub adds subs cmp cmn neg mul madd msub sdiv udiv
  logic/shift and orr eor ands tst bic lsl lsr asr   (second operand: register, #imm, shifted register)
  memory      ldr str ldrb strb ldrh strh ldrsw ldp stp   [x1]  [x1, #8]  [x1, #8]!  [x1], #8  [x1, x2, lsl #3]
  branches    b bl br blr ret b.eq b.ne b.lt b.le b.gt b.ge b.lo b.ls b.hi b.hs b.mi b.pl b.vs b.vc cbz cbnz
  conditions  cset csel
  system      svc #0 with Linux numbers in x8:  63 read(fd,buf,len)  64 write(fd,buf,len)  93/94 exit(code)
  start       the label _start or main, otherwise the first instruction; "ret" from main ends the program.

Example:
    .data
    msg:  .asciz "Hello from ARM64\\n"
    .text
    _start:
        mov x0, #1
        ldr x1, =msg
        mov x2, #17
        mov x8, #64
        svc #0
        mov x0, #0
        mov x8, #93
        svc #0
"""
import re
import sys

VERSION = "0.2"

CODE_BASE = 0x400000
DATA_BASE = 0x10000000
STACK_TOP = 0x7FF00000
EXIT_ADDR = 0xDEAD0000          # the return address of main: "ret" there ends the program
M64 = (1 << 64) - 1
M32 = (1 << 32) - 1
DEFAULT_STEPS = 1000000
DEFAULT_STACK_KB = 256
MAX_DATA = 16 * 1024 * 1024
MAX_WRITE = 1 << 20


class AsmError(Exception):
    def __init__(self, msg, line=None):
        Exception.__init__(self, msg)
        self.msg, self.line = msg, line


LINE = {"start": True}          # is the output at the start of a line? ([INFO]/[ERROR] must begin a line)


def say(text):
    if not LINE["start"]:
        print()
        LINE["start"] = True
    print(text)


def mask(bits):
    return M64 if bits == 64 else M32


def signed(v, bits):
    v &= mask(bits)
    return v - (1 << bits) if v >> (bits - 1) else v


def unescape(s, line=None):
    out, i = [], 0
    table = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            if s[i + 1] not in table:
                raise AsmError("unknown escape \\%s" % s[i + 1], line)
            out.append(table[s[i + 1]])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def strip_comment(line):
    if line.lstrip().startswith("#"):                   # a whole-line comment; '#' inside a line is the immediate prefix
        return ""
    out, quote, i = [], None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            out.append(ch)
        elif ch == ";" or line.startswith("//", i):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def split_operands(text):
    """'x0, [x1, #8]!, lsl #2' -> ['x0', '[x1, #8]!', 'lsl #2']"""
    parts, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            cur.append(ch)
        elif ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    last = "".join(cur).strip()
    if last or parts:
        parts.append(last)
    return parts


CONDS = {
    "eq": lambda n, z, c, v: z, "ne": lambda n, z, c, v: not z,
    "cs": lambda n, z, c, v: c, "hs": lambda n, z, c, v: c,
    "cc": lambda n, z, c, v: not c, "lo": lambda n, z, c, v: not c,
    "mi": lambda n, z, c, v: n, "pl": lambda n, z, c, v: not n,
    "vs": lambda n, z, c, v: v, "vc": lambda n, z, c, v: not v,
    "hi": lambda n, z, c, v: c and not z, "ls": lambda n, z, c, v: (not c) or z,
    "ge": lambda n, z, c, v: n == v, "lt": lambda n, z, c, v: n != v,
    "gt": lambda n, z, c, v: (not z) and n == v, "le": lambda n, z, c, v: z or n != v,
    "al": lambda n, z, c, v: True,
}

REG_RE = re.compile(r"^([xw])(\d+)$")
SHIFT_RE = re.compile(r"^(lsl|lsr|asr)\s*#?\s*(\S+)$", re.I)
LABEL_RE = re.compile(r"^([A-Za-z_.$][\w.$]*)\s*:")


class Program:
    """The assembled text: instructions, labels and the initial data."""

    def __init__(self):
        self.code = []              # (mnemonic, [operands], source line number, source text)
        self.labels = {}            # name -> absolute address
        self.equ = {}
        self.data = bytearray()
        self.fixups = []            # (offset, size, expression, line)
        self.entry = 0
        self.pending_code_labels = []


def assemble(text):
    prog = Program()
    section = ".text"
    data_labels = {}
    code_labels = {}
    for no, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw)
        while True:
            m = LABEL_RE.match(line)
            if not m:
                break
            name = m.group(1)
            if name in data_labels or name in code_labels:
                raise AsmError("label '%s' is defined twice" % name, no)
            if section == ".text":
                code_labels[name] = len(prog.code)
            else:
                data_labels[name] = len(prog.data)
            line = line[m.end():].strip()
        if not line:
            continue
        pieces = line.split(None, 1)                     # a space OR a tab separates the mnemonic from the operands
        head_l = pieces[0].lower()
        rest = pieces[1].strip() if len(pieces) > 1 else ""
        head = pieces[0]
        if head_l.startswith("."):
            section = directive(prog, head_l, rest, section, no)
            continue
        if section != ".text":
            raise AsmError("instruction '%s' is not allowed in %s" % (head, section), no)
        prog.code.append((head_l, split_operands(rest), no, line))
    for name, i in code_labels.items():
        prog.labels[name] = CODE_BASE + 4 * i
    for name, off in data_labels.items():
        prog.labels[name] = DATA_BASE + off
    for start in ("_start", "main"):
        if start in code_labels:
            prog.entry = code_labels[start]
            break
    return prog


def directive(prog, name, rest, section, no):
    if name in (".text", ".data", ".bss", ".rodata"):
        return name
    if name in (".global", ".globl", ".type", ".size", ".file", ".section", ".p2align_", ".ident"):
        if name == ".section" and rest:
            return rest.split(",")[0].strip() if rest.split(",")[0].strip() in (".text", ".data", ".bss", ".rodata") else ".data"
        return section
    if name in (".equ", ".set"):
        parts = split_operands(rest)
        if len(parts) != 2:
            raise AsmError("%s needs a name and a value" % name, no)
        prog.equ[parts[0]] = parts[1]
        return section
    if section == ".text":
        raise AsmError("data directive %s is not allowed in .text (put it in .data)" % name, no)
    data = prog.data
    if name in (".asciz", ".string", ".ascii"):
        for item in split_operands(rest):
            if not (len(item) >= 2 and item[0] == item[-1] == '"'):
                raise AsmError("%s needs a string in quotes" % name, no)
            data += unescape(item[1:-1], no).encode("utf-8")
            if name != ".ascii":
                data.append(0)
    elif name in (".byte", ".hword", ".short", ".word", ".int", ".long", ".quad", ".xword"):
        size = {".byte": 1, ".hword": 2, ".short": 2, ".word": 4, ".int": 4, ".long": 4, ".quad": 8, ".xword": 8}[name]
        for item in split_operands(rest):
            prog.fixups.append((len(data), size, item, no))
            data += bytes(size)
    elif name in (".space", ".zero", ".skip"):
        n = int(rest.split(",")[0], 0)
        if n < 0 or len(data) + n > MAX_DATA:
            raise AsmError("%s: bad size" % name, no)
        data += bytes(n)
    elif name in (".align", ".balign", ".p2align"):
        n = int(rest.split(",")[0], 0)
        step = (1 << n) if name != ".balign" else n
        while len(data) % max(step, 1):
            data.append(0)
    else:
        raise AsmError("unknown directive %s" % name, no)
    if len(data) > MAX_DATA:
        raise AsmError("the data is too big", no)
    return section


class Cpu:
    def __init__(self, prog, steps, stack_kb, trace, stdin_text):
        self.prog = prog
        self.regs = [0] * 33                    # 0..30 = x0..x30, 31 = zero register, 32 = sp
        self.n = self.z = self.c = self.v = False
        self.pc = prog.entry
        self.steps_left, self.steps = steps, 0
        self.trace = trace
        self.stack = bytearray(stack_kb * 1024)
        self.stack_base = STACK_TOP - len(self.stack)
        self.data = prog.data
        self.regs[32] = STACK_TOP
        self.regs[30] = EXIT_ADDR
        self.stdin = stdin_text
        self.exit_code = None
        self.line = None
        for off, size, expr, no in prog.fixups:
            v = self.ev(expr, no) & ((1 << (8 * size)) - 1)
            self.data[off:off + size] = v.to_bytes(size, "little")

    # ---------- values ----------
    def symval(self, name, line=None):
        if name in self.prog.labels:
            return self.prog.labels[name]
        if name in self.prog.equ:
            return self.ev(self.prog.equ[name], line)
        raise AsmError("unknown symbol '%s'" % name, line)

    def ev(self, text, line=None):
        s = text.strip()
        if s.startswith("#"):
            s = s[1:].strip()
        m = re.match(r"^:lo12:(.+)$", s)
        if m:
            return self.ev(m.group(1), line) & 0xFFF
        s = re.sub(r"'(\\?.)'", lambda mm: str(ord(unescape(mm.group(1), line))), s)
        s = re.sub(r"(?<![\w.$])[A-Za-z_.$][\w.$]*", lambda mm: str(self.symval(mm.group(0), line)), s)
        s = s.replace("/", "//")
        if not re.fullmatch(r"[0-9a-fA-FxX+\-*()<>&|~ /]+", s):
            raise AsmError("cannot understand '%s'" % text, line)
        try:
            return int(eval(s, {"__builtins__": {}}, {}))
        except Exception:
            raise AsmError("cannot understand '%s'" % text, line)

    def reg(self, tok, line):
        t = tok.strip().lower()
        if t in ("sp", "wsp"):
            return 32, 64 if t == "sp" else 32
        if t in ("xzr", "wzr"):
            return 31, 64 if t == "xzr" else 32
        if t == "lr":
            return 30, 64
        if t == "fp":
            return 29, 64
        m = REG_RE.match(t)
        if m and int(m.group(2)) <= 30:
            return int(m.group(2)), 64 if m.group(1) == "x" else 32
        raise AsmError("'%s' is not a register" % tok, line)

    def is_reg(self, tok):
        try:
            self.reg(tok, None)
            return True
        except AsmError:
            return False

    def get(self, idx, bits):
        return 0 if idx == 31 else self.regs[idx] & mask(bits)

    def put(self, idx, bits, value):
        if idx != 31:
            self.regs[idx] = value & mask(bits)      # a w-register write clears the upper half, like the real CPU

    def shifted(self, value, tok, bits, line):
        m = SHIFT_RE.match(tok.strip())
        if not m:
            raise AsmError("expected a shift like 'lsl #2', got '%s'" % tok, line)
        kind, n = m.group(1).lower(), self.ev(m.group(2), line)
        if not 0 <= n < bits:
            raise AsmError("shift out of range", line)
        if kind == "lsl":
            return (value << n) & mask(bits)
        if kind == "lsr":
            return value >> n
        return (signed(value, bits) >> n) & mask(bits)

    def operand2(self, toks, bits, line):
        """register / #imm, with an optional shift -> value"""
        if not toks:
            raise AsmError("missing operand", line)
        if self.is_reg(toks[0]):
            idx, _ = self.reg(toks[0], line)
            v = self.get(idx, bits)
        else:
            v = self.ev(toks[0], line) & mask(bits)
        if len(toks) > 1:
            v = self.shifted(v, toks[1], bits, line)
        return v

    # ---------- memory ----------
    def segment(self, addr, size, line):
        if DATA_BASE <= addr and addr + size <= DATA_BASE + len(self.data):
            return self.data, addr - DATA_BASE
        if self.stack_base <= addr and addr + size <= STACK_TOP:
            return self.stack, addr - self.stack_base
        raise AsmError("segmentation fault: bad memory address 0x%x (%d byte(s))" % (addr, size), line)

    def mem_read(self, addr, size, line):
        seg, off = self.segment(addr, size, line)
        return int.from_bytes(seg[off:off + size], "little")

    def mem_write(self, addr, size, value, line):
        seg, off = self.segment(addr, size, line)
        seg[off:off + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")

    def address(self, toks, line):
        """[x1] / [x1, #8] / [x1, #8]! / [x1], #8 / [x1, x2, lsl #3] -> (address, (base index, new base value) or None)"""
        first = toks[0].strip()
        wb = first.endswith("!")
        if wb:
            first = first[:-1]
        if not (first.startswith("[") and first.endswith("]")):
            raise AsmError("expected a memory operand like [x1, #8], got '%s'" % toks[0], line)
        inner = split_operands(first[1:-1])
        base, _ = self.reg(inner[0], line)
        base_val = self.get(base, 64)
        if len(toks) > 1:                                   # post-index: [x1], #8
            delta = self.ev(toks[1], line)
            return base_val, (base, (base_val + delta) & M64)
        off = 0
        if len(inner) > 1:
            if self.is_reg(inner[1]):
                ridx, rbits = self.reg(inner[1], line)
                off = self.get(ridx, rbits)
                if len(inner) > 2:
                    off = self.shifted(off, inner[2], 64, line)
            else:
                off = self.ev(inner[1], line)
        addr = (base_val + off) & M64
        return addr, ((base, addr) if wb else None)

    # ---------- flags ----------
    def add_flags(self, a, b, carry, bits):
        mk = mask(bits)
        total = a + b + carry
        res = total & mk
        self.n = bool(res >> (bits - 1))
        self.z = res == 0
        self.c = total > mk
        sa, sb = signed(a, bits), signed(b, bits)
        self.v = not (-(1 << (bits - 1)) <= sa + sb + carry < (1 << (bits - 1)))
        return res

    def cond(self, name, line):
        f = CONDS.get(name)
        if f is None:
            raise AsmError("unknown condition '%s'" % name, line)
        return f(self.n, self.z, self.c, self.v)

    # ---------- branches ----------
    def target(self, tok, line):
        name = tok.strip()
        if name not in self.prog.labels:
            raise AsmError("unknown label '%s'" % name, line)
        addr = self.prog.labels[name]
        if not (CODE_BASE <= addr < CODE_BASE + 4 * len(self.prog.code)):
            raise AsmError("'%s' is data, not code" % name, line)
        return (addr - CODE_BASE) // 4

    def jump_to_address(self, addr, line):
        if addr == EXIT_ADDR:
            self.exit_code = self.get(0, 64) & 0xFF
            return
        if not (CODE_BASE <= addr < CODE_BASE + 4 * len(self.prog.code)) or addr % 4:
            raise AsmError("jump to a bad address 0x%x" % addr, line)
        self.pc = (addr - CODE_BASE) // 4

    # ---------- the run loop ----------
    def run(self):
        code = self.prog.code
        while self.exit_code is None:
            if self.pc >= len(code):
                raise AsmError("the program ran past its last instruction (forgot the exit system call or ret?)",
                               code[-1][2] if code else None)
            if self.steps_left <= 0:
                raise AsmError("stopped after %d instructions (raise the limit with --steps N)" % self.steps, self.line)
            op, toks, line, text = code[self.pc]
            self.line = line
            self.steps += 1
            self.steps_left -= 1
            if self.trace:
                say("[TRACE] %06x  line %-4d %s" % (CODE_BASE + 4 * self.pc, line, text))
            self.pc += 1
            self.execute(op, toks, line)
        return self.exit_code

    def execute(self, op, t, line):
        try:
            handler = getattr(self, "i_" + op.replace(".", "_"), None)
            if handler is None:
                if op.startswith("b."):
                    return self.branch_cond(op[2:], t, line)
                raise AsmError("unknown instruction '%s'" % op, line)
            handler(t, line)
        except IndexError:
            raise AsmError("wrong number of operands for '%s'" % op, line)

    def rd_rn(self, t, line):
        rd, bits = self.reg(t[0], line)
        rn, _ = self.reg(t[1], line)
        return rd, rn, bits

    # --- moves ---
    def i_mov(self, t, line):
        rd, bits = self.reg(t[0], line)
        if self.is_reg(t[1]):
            rn, _ = self.reg(t[1], line)
            self.put(rd, bits, self.get(rn, bits))
        else:
            self.put(rd, bits, self.ev(t[1], line))

    def i_mvn(self, t, line):
        rd, bits = self.reg(t[0], line)
        self.put(rd, bits, ~self.operand2(t[1:], bits, line))

    def _wide(self, t, line):
        rd, bits = self.reg(t[0], line)
        imm = self.ev(t[1], line)
        shift = 0
        if len(t) > 2:
            m = SHIFT_RE.match(t[2].strip())
            if not m or m.group(1).lower() != "lsl":
                raise AsmError("expected 'lsl #n'", line)
            shift = self.ev(m.group(2), line)
        if imm < 0 or imm > 0xFFFF or shift % 16 or shift >= bits:
            raise AsmError("bad immediate or shift for movz/movk/movn", line)
        return rd, bits, imm, shift

    def i_movz(self, t, line):
        rd, bits, imm, shift = self._wide(t, line)
        self.put(rd, bits, imm << shift)

    def i_movn(self, t, line):
        rd, bits, imm, shift = self._wide(t, line)
        self.put(rd, bits, ~(imm << shift))

    def i_movk(self, t, line):
        rd, bits, imm, shift = self._wide(t, line)
        self.put(rd, bits, (self.get(rd, bits) & ~(0xFFFF << shift)) | (imm << shift))

    def i_adr(self, t, line):
        rd, _ = self.reg(t[0], line)
        self.put(rd, 64, self.ev(t[1], line))

    def i_adrp(self, t, line):
        rd, _ = self.reg(t[0], line)
        self.put(rd, 64, self.ev(t[1], line) & ~0xFFF)

    # --- arithmetic ---
    def _addsub(self, t, line, sub, setf, rd_override=None):
        rd, rn, bits = self.rd_rn(t, line)
        b = self.operand2(t[2:], bits, line)
        a = self.get(rn, bits)
        if sub:
            res = self.add_flags(a, (~b) & mask(bits), 1, bits)
        else:
            res = self.add_flags(a, b, 0, bits)
        if not setf:
            pass                                     # flags were computed but must stay unchanged
        return rd, bits, res

    def _arith(self, t, line, sub, setf):
        saved = (self.n, self.z, self.c, self.v)
        rd, bits, res = self._addsub(t, line, sub, setf)
        if not setf:
            self.n, self.z, self.c, self.v = saved
        self.put(rd, bits, res)

    def i_add(self, t, line): self._arith(t, line, False, False)
    def i_sub(self, t, line): self._arith(t, line, True, False)
    def i_adds(self, t, line): self._arith(t, line, False, True)
    def i_subs(self, t, line): self._arith(t, line, True, True)

    def i_cmp(self, t, line): self._arith(["xzr" if self.reg(t[0], line)[1] == 64 else "wzr"] + t, line, True, True)
    def i_cmn(self, t, line): self._arith(["xzr" if self.reg(t[0], line)[1] == 64 else "wzr"] + t, line, False, True)

    def i_neg(self, t, line):
        rd, bits = self.reg(t[0], line)
        self._arith([t[0], "xzr" if bits == 64 else "wzr"] + t[1:], line, True, False)

    def i_mul(self, t, line):
        rd, bits = self.reg(t[0], line)
        a, b = (self.get(self.reg(t[i], line)[0], bits) for i in (1, 2))
        self.put(rd, bits, a * b)

    def i_madd(self, t, line):
        rd, bits = self.reg(t[0], line)
        a, b, c = (self.get(self.reg(t[i], line)[0], bits) for i in (1, 2, 3))
        self.put(rd, bits, a * b + c)

    def i_msub(self, t, line):
        rd, bits = self.reg(t[0], line)
        a, b, c = (self.get(self.reg(t[i], line)[0], bits) for i in (1, 2, 3))
        self.put(rd, bits, c - a * b)

    def i_sdiv(self, t, line):
        rd, bits = self.reg(t[0], line)
        a, b = (signed(self.get(self.reg(t[i], line)[0], bits), bits) for i in (1, 2))
        q = 0 if b == 0 else abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)    # truncates toward zero
        self.put(rd, bits, q)

    def i_udiv(self, t, line):
        rd, bits = self.reg(t[0], line)
        a, b = (self.get(self.reg(t[i], line)[0], bits) for i in (1, 2))
        self.put(rd, bits, 0 if b == 0 else a // b)

    # --- logic and shifts ---
    def _logic(self, t, line, fn, setf=False, invert=False):
        rd, rn, bits = self.rd_rn(t, line)
        b = self.operand2(t[2:], bits, line)
        if invert:
            b = ~b & mask(bits)
        res = fn(self.get(rn, bits), b) & mask(bits)
        if setf:
            self.n, self.z, self.c, self.v = bool(res >> (bits - 1)), res == 0, False, False
        self.put(rd, bits, res)

    def i_and(self, t, line): self._logic(t, line, lambda a, b: a & b)
    def i_orr(self, t, line): self._logic(t, line, lambda a, b: a | b)
    def i_eor(self, t, line): self._logic(t, line, lambda a, b: a ^ b)
    def i_bic(self, t, line): self._logic(t, line, lambda a, b: a & b, invert=True)
    def i_ands(self, t, line): self._logic(t, line, lambda a, b: a & b, setf=True)

    def i_tst(self, t, line):
        bits = self.reg(t[0], line)[1]
        self._logic(["xzr" if bits == 64 else "wzr"] + t, line, lambda a, b: a & b, setf=True)

    def _shift(self, t, line, kind):
        rd, rn, bits = self.rd_rn(t, line)
        a = self.get(rn, bits)
        n = self.get(self.reg(t[2], line)[0], 64) % bits if self.is_reg(t[2]) else self.ev(t[2], line)
        if not 0 <= n < bits:
            raise AsmError("shift out of range", line)
        if kind == "lsl":
            res = a << n
        elif kind == "lsr":
            res = a >> n
        else:
            res = signed(a, bits) >> n
        self.put(rd, bits, res)

    def i_lsl(self, t, line): self._shift(t, line, "lsl")
    def i_lsr(self, t, line): self._shift(t, line, "lsr")
    def i_asr(self, t, line): self._shift(t, line, "asr")

    def i_cset(self, t, line):
        rd, bits = self.reg(t[0], line)
        self.put(rd, bits, 1 if self.cond(t[1].strip().lower(), line) else 0)

    def i_csel(self, t, line):
        rd, bits = self.reg(t[0], line)
        pick = t[1] if self.cond(t[3].strip().lower(), line) else t[2]
        self.put(rd, bits, self.get(self.reg(pick, line)[0], bits))

    def i_nop(self, t, line):
        pass

    # --- memory ---
    def _load(self, t, line, size, sign=False):
        rt, bits = self.reg(t[0], line)
        if size is None:
            size = bits // 8
        if len(t) == 2 and t[1].strip().startswith("="):
            self.put(rt, bits, self.ev(t[1].strip()[1:], line))
            return
        addr, wb = self.address(t[1:], line)
        v = self.mem_read(addr, size, line)
        if sign:
            v = signed(v, 8 * size) & mask(bits)
        self.put(rt, bits, v)
        if wb:
            self.put(wb[0], 64, wb[1])

    def _store(self, t, line, size):
        rt, bits = self.reg(t[0], line)
        if size is None:
            size = bits // 8
        addr, wb = self.address(t[1:], line)
        self.mem_write(addr, size, self.get(rt, bits), line)
        if wb:
            self.put(wb[0], 64, wb[1])

    def i_ldr(self, t, line): self._load(t, line, None)
    def i_ldrb(self, t, line): self._load(t, line, 1)
    def i_ldrh(self, t, line): self._load(t, line, 2)
    def i_ldrsw(self, t, line): self._load(t, line, 4, sign=True)
    def i_str(self, t, line): self._store(t, line, None)
    def i_strb(self, t, line): self._store(t, line, 1)
    def i_strh(self, t, line): self._store(t, line, 2)

    def i_ldp(self, t, line):
        r1, bits = self.reg(t[0], line)
        r2, _ = self.reg(t[1], line)
        size = bits // 8
        addr, wb = self.address(t[2:], line)
        v1, v2 = self.mem_read(addr, size, line), self.mem_read(addr + size, size, line)
        self.put(r1, bits, v1)
        self.put(r2, bits, v2)
        if wb:
            self.put(wb[0], 64, wb[1])

    def i_stp(self, t, line):
        r1, bits = self.reg(t[0], line)
        r2, _ = self.reg(t[1], line)
        size = bits // 8
        addr, wb = self.address(t[2:], line)
        self.mem_write(addr, size, self.get(r1, bits), line)
        self.mem_write(addr + size, size, self.get(r2, bits), line)
        if wb:
            self.put(wb[0], 64, wb[1])

    # --- branches ---
    def i_b(self, t, line): self.pc = self.target(t[0], line)

    def i_bl(self, t, line):
        self.regs[30] = CODE_BASE + 4 * self.pc
        self.pc = self.target(t[0], line)

    def i_br(self, t, line): self.jump_to_address(self.get(self.reg(t[0], line)[0], 64), line)

    def i_blr(self, t, line):
        dest = self.get(self.reg(t[0], line)[0], 64)
        self.regs[30] = CODE_BASE + 4 * self.pc
        self.jump_to_address(dest, line)

    def i_ret(self, t, line):
        self.jump_to_address(self.get(self.reg(t[0], line)[0] if t else 30, 64), line)

    def branch_cond(self, name, t, line):
        if self.cond(name, line):
            self.pc = self.target(t[0], line)

    def i_cbz(self, t, line):
        rt, bits = self.reg(t[0], line)
        if self.get(rt, bits) == 0:
            self.pc = self.target(t[1], line)

    def i_cbnz(self, t, line):
        rt, bits = self.reg(t[0], line)
        if self.get(rt, bits) != 0:
            self.pc = self.target(t[1], line)

    # --- system calls (Linux numbers) ---
    def i_svc(self, t, line):
        num = self.get(8, 64)
        if num == 64:                                   # write(fd, buf, len)
            fd, buf, n = self.get(0, 64), self.get(1, 64), self.get(2, 64)
            if fd not in (1, 2):
                self.put(0, 64, -9)                     # EBADF
                return
            if n > MAX_WRITE:
                raise AsmError("write of %d bytes is too big" % n, line)
            seg, off = self.segment(buf, n, line) if n else (b"", 0)
            out = bytes(seg[off:off + n]).decode("utf-8", "replace")
            sys.stdout.write(out)
            if out:
                LINE["start"] = out.endswith("\n")
            self.put(0, 64, n)
        elif num == 63:                                 # read(fd, buf, len)
            fd, buf, n = self.get(0, 64), self.get(1, 64), self.get(2, 64)
            if fd != 0:
                self.put(0, 64, -9)
                return
            chunk, self.stdin = self.stdin[:n], self.stdin[n:]
            if chunk:
                seg, off = self.segment(buf, len(chunk), line)
                seg[off:off + len(chunk)] = chunk
            self.put(0, 64, len(chunk))
        elif num in (93, 94):                           # exit(code)
            self.exit_code = self.get(0, 64) & 0xFF
        else:
            raise AsmError("system call %d is not supported (use 63 read, 64 write, 93 exit)" % num, line)


def show_regs(cpu):
    say("[INFO] registers:")
    rows = []
    for i in range(31):
        rows.append("x%-2d= 0x%016x" % (i, cpu.regs[i]))
    rows.append("sp = 0x%016x" % cpu.regs[32])
    for i in range(0, len(rows), 2):
        say("  " + "   ".join(rows[i:i + 2]))
    say("  flags: N=%d Z=%d C=%d V=%d" % (cpu.n, cpu.z, cpu.c, cpu.v))


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        say("asm %s - run AArch64 assembly in a safe emulator" % VERSION)
        say("  py asm.py prog.s [--trace] [--regs] [--steps N] [--stack KB] [--input \"text\"]")
        return 0
    path, trace, regs, steps, stack_kb, stdin = args[0], False, False, DEFAULT_STEPS, DEFAULT_STACK_KB, ""
    i = 1
    try:
        while i < len(args):
            a = args[i]
            if a == "--trace":
                trace = True
            elif a == "--regs":
                regs = True
            elif a in ("--steps", "--stack", "--input"):
                i += 1
                if i >= len(args):
                    raise ValueError("%s needs a value" % a)
                if a == "--steps":
                    steps = int(args[i])
                elif a == "--stack":
                    stack_kb = int(args[i])
                else:
                    stdin = unescape(args[i])
            else:
                raise ValueError("unknown option '%s'" % a)
            i += 1
        if steps < 1 or not 1 <= stack_kb <= 4096:
            raise ValueError("--steps must be at least 1 and --stack 1..4096 KB")
    except (ValueError, AsmError) as e:
        say("[ERROR] %s" % (e.msg if isinstance(e, AsmError) else e))
        return 1
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        say("[ERROR] cannot read %s: %s" % (path, e.strerror or e))
        return 1
    cpu = None
    try:
        prog = assemble(text)
        if not prog.code:
            raise AsmError("the program has no instructions")
        cpu = Cpu(prog, steps, stack_kb, trace, stdin.encode("utf-8"))
        code = cpu.run()
    except AsmError as e:
        sys.stdout.flush()
        where = "%s:%d: " % (path, e.line) if e.line else "%s: " % path
        say("[ERROR] " + where + e.msg)
        if cpu and regs:
            show_regs(cpu)
        return 1
    sys.stdout.flush()
    say("[INFO] exit code %d (%d instructions)" % (code, cpu.steps))
    if regs:
        show_regs(cpu)
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv)
    except KeyboardInterrupt:
        say("[ERROR] stopped")
        rc = 1
    except Exception as e:                              # a bug in asm.py itself: report it instead of dying silently
        say("[ERROR] internal error in asm.py: %s: %s" % (type(e).__name__, e))
        rc = 1
    try:
        sys.stdout.flush()
    except Exception:
        pass
    if rc:                                              # exit only on failure: some hosts (IDE consoles, app runners)
        sys.exit(rc)                                    # treat even sys.exit(0) as "close everything"
