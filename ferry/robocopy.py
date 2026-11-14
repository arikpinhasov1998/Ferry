"""Translate robocopy command lines into a Ferry job.

    ferry robo <source> <dest> [file [file]...] [options]

Semantics follow robocopy: without /S, /E or /MIR only the top folder is copied,
symlinks are followed unless /SL is given, and the exit code is robocopy's
bitmask (0/1 = success, >= 8 = failures).
"""
from __future__ import annotations

import datetime as _dt
import re

KNOWN_DISPLAY = {"NP", "NFL", "NDL", "NJH", "NJS", "V", "ETA", "BYTES", "X", "TS", "FP", "NS", "NC", "TEE",
                 "UNICODE", "UNILOG", "Z", "ZB", "J", "EFSRAW", "NOOFFLOAD", "COMPRESS", "CREATE", "DST",
                 "PF", "R", "W", "REG", "TBD", "QUIT", "NOSD", "NODD", "DEBUG", "SPARSE"}


class RoboArgs:
    def __init__(self):
        self.spec: dict = {"metadata": {"symlinks": "follow"}, "filters": {}, "mode": "copy",
                           "delete_mode": "delete", "empty_dirs": True}
        self.log_file: str | None = None
        self.log_append = False
        self.tee = False
        self.warnings: list[str] = []


def _days_or_date(v: str) -> str:
    if len(v) == 8 and v.isdigit():
        d = _dt.datetime.strptime(v, "%Y%m%d")
        days = max(0, (_dt.datetime.now() - d).days)
        return f"{days}d"
    return f"{int(v)}d"


def parse(argv: list[str]) -> RoboArgs:
    r = RoboArgs()
    spec, meta, flt = r.spec, r.spec["metadata"], r.spec["filters"]
    positional, opts = [], []
    for tok in argv:
        if re.match(r"^/[A-Za-z+]", tok) and len(positional) >= 2:
            opts.append(tok)
        elif opts and opts[-1].upper() in ("/XF", "/XD", "/IF") or (opts and opts[-1].startswith("+")):
            opts.append("+" + tok)   # argument list of /XF /XD /IF
        else:
            positional.append(tok)
    if len(positional) < 2:
        raise ValueError("usage: ferry robo <source> <destination> [file ...] [options]")
    spec["source"], spec["dest"] = positional[0], positional[1]
    files = [f for f in positional[2:] if f not in ("*.*", "*")]
    if files:
        flt["include"] = files

    recursive = False
    xo = xn = xc = False
    list_target = None
    for tok in opts:
        if tok.startswith("+"):
            val = tok[1:]
            if list_target == "XF":
                flt.setdefault("exclude", []).append(val)
            elif list_target == "XD":
                flt.setdefault("exclude_dirs", []).append(val)
            elif list_target == "IF":
                flt.setdefault("include", []).append(val)
            continue
        name, _, val = tok[1:].partition(":")
        u = name.upper()
        list_target = u if u in ("XF", "XD", "IF") else None
        if u in ("XF", "XD", "IF"):
            continue
        if u == "S":
            recursive, spec["empty_dirs"] = True, False
        elif u == "E":
            recursive, spec["empty_dirs"] = True, True
        elif u == "LEV" and val:
            flt["max_depth"] = max(0, int(val) - 1)
        elif u in ("MIR", "PURGE"):
            spec["mode"] = "mirror"
            if u == "MIR":
                recursive, spec["empty_dirs"] = True, True
        elif u == "MOV":
            spec["mode"] = "move"
        elif u == "MOVE":
            spec["mode"], spec["move_dirs"] = "move", True
        elif u in ("COPY", "COPYALL", "SEC", "NOCOPY"):
            flags = {"COPYALL": "DATSOU", "SEC": "DATS", "NOCOPY": ""}.get(u, val.upper() or "DAT")
            meta["timestamps"] = "T" in flags
            meta["permissions"] = "A" in flags
            meta["acl"] = "S" in flags
            meta["owner"] = "O" in flags or "U" in flags
            if u == "NOCOPY":
                r.warnings.append("/NOCOPY is not supported; file data is always copied")
        elif u == "DCOPY":
            pass   # folder timestamps/attributes are always copied
        elif u == "SL":
            meta["symlinks"] = "copy"
        elif u == "SJ":
            meta["symlinks"] = "copy"
        elif u in ("XJ", "XJD", "XJF"):
            if meta.get("symlinks") != "copy":
                meta["symlinks"] = "skip"
        elif u == "XO":
            xo = True
        elif u == "XN":
            xn = True
        elif u == "XC":
            xc = True
        elif u in ("IS", "IT", "IM"):
            spec["overwrite"] = "always"
        elif u == "XX":
            spec["mode"] = "copy" if spec["mode"] == "mirror" else spec["mode"]
        elif u == "XL":
            spec["skip_new"] = True
        elif u == "MAX":
            flt["max_size"] = int(val)
        elif u == "MIN":
            flt["min_size"] = int(val)
        elif u in ("MAXAGE", "MAXLAD"):
            flt["max_age"] = _days_or_date(val)
        elif u in ("MINAGE", "MINLAD"):
            flt["min_age"] = _days_or_date(val)
        elif u == "R" and val:
            spec["retries"] = min(int(val), 1000)
        elif u == "W" and val:
            spec["retry_wait"] = int(val)
        elif u == "MT":
            spec["threads"] = int(val) if val else 8
        elif u == "L":
            spec["dry_run"] = True
        elif u in ("LOG", "UNILOG"):
            r.log_file, r.log_append = val, False
        elif u in ("LOG+", "UNILOG+"):
            r.log_file, r.log_append = val, True
        elif tok[1:].upper().startswith(("LOG+:", "UNILOG+:")):
            r.log_file, r.log_append = tok.split(":", 1)[1], True
        elif u == "TEE":
            r.tee = True
        elif u == "IPG" and val:
            gap_ms = max(1, int(val))
            spec["bwlimit"] = str(int(65536 * 1000 / gap_ms))
        elif u == "RH" and val:
            m = re.fullmatch(r"(\d{2})(\d{2})-(\d{2})(\d{2})", val)
            if not m:
                raise ValueError("/RH expects hhmm-hhmm")
            spec["windows"] = [f"{m.group(1)}:{m.group(2)}-{m.group(3)}:{m.group(4)}"]
        elif u == "FFT":
            spec["modify_window"] = 2.0
        elif u in ("MON", "MOT"):
            spec["watch"] = True
            r.warnings.append(f"/{u}: use a saved job with --watch for continuous sync")
        elif u == "B":
            r.warnings.append("/B backup mode: run Ferry as administrator to read files without access rights")
        elif u in ("A+", "A-", "IA", "XA", "A", "M"):
            r.warnings.append(f"/{name} (attribute filters/changes) is not supported and was ignored")
        elif u in KNOWN_DISPLAY:
            pass
        elif u in ("JOB", "SAVE", "NOSD", "NODD"):
            r.warnings.append(f"/{name}: use 'ferry job add' to save jobs")
        else:
            r.warnings.append(f"unknown robocopy option {tok} ignored")
    if not recursive and "max_depth" not in flt:
        flt["max_depth"] = 0
    if xo and xn and xc:
        spec["overwrite"] = "never"
    elif xo:
        spec["overwrite"] = "newer"
    return r


def exit_code(info: dict) -> int:
    """Robocopy-style exit bitmask."""
    if info["phase"] == "failed":
        return 16
    code = 0
    if info.get("done_files"):
        code |= 1
    if info.get("deleted"):
        code |= 2
    if info.get("failed") or info["phase"] in ("cancelled", "completed_with_errors"):
        code |= 8
    return code
