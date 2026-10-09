#!/usr/bin/env python3
"""檢查舊系統（例如凌越）備份檔的格式與內容結構。

用法：python tools/inspect_backup.py wstkBKUP.001
      python tools/inspect_backup.py wstkBKUP.001 --extract 輸出資料夾   （能解開的話，順便解出來）

預設只列出「格式、資料表名稱、欄位名稱、筆數」，不會印出客戶資料內容，
可以放心把結果貼給別人看。加 --sample 才會印出每個資料表前 3 筆。
"""
import argparse
import gzip
import io
import os
import struct
import sys
import tarfile
import zipfile

SIGNATURES = [
    (b"PK\x03\x04", "ZIP 壓縮檔"),
    (b"Rar!\x1a\x07", "RAR 壓縮檔"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip 壓縮檔"),
    (b"MSCF", "Microsoft CAB 壓縮檔"),
    (b"\x1f\x8b", "GZIP 壓縮檔"),
    (b"\x60\xea", "ARJ 壓縮檔"),
    (b"BZh", "BZIP2 壓縮檔"),
    (b"\x00\x01\x00\x00Standard Jet DB", "Microsoft Access 資料庫 (.mdb)"),
    (b"\x00\x01\x00\x00Standard ACE DB", "Microsoft Access 資料庫 (.accdb)"),
    (b"TAPE", "Microsoft SQL Server 備份 (.bak)"),
    (b"SQLite format 3\x00", "SQLite 資料庫"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "Microsoft OLE 複合文件（舊版 Office / 部分資料庫）"),
]
DBF_VERSIONS = {0x02, 0x03, 0x30, 0x31, 0x32, 0x43, 0x63, 0x83, 0x8B, 0x8E, 0xCB, 0xF5, 0xFB}


def decode(b):
    for enc in ("cp950", "big5hkscs", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("cp950", errors="replace")


def parse_dbf_header(buf, off=0):
    """若 buf[off:] 看起來是 DBF 檔頭，回傳 (欄位清單, 筆數, 檔頭長, 每筆長度)，否則 None。"""
    if off + 32 > len(buf) or buf[off] not in DBF_VERSIONS:
        return None
    yy, mm, dd = buf[off + 1:off + 4]
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    n_rec, hdr_len, rec_len = struct.unpack("<IHH", buf[off + 4:off + 12])
    if hdr_len < 65 or rec_len < 2 or n_rec > 50_000_000:
        return None
    fields, pos = [], off + 32
    while pos + 32 <= len(buf) and buf[pos] != 0x0D:
        raw_name = buf[pos:pos + 11].split(b"\0")[0]
        ftype = chr(buf[pos + 11])
        if not raw_name or ftype not in "CNFDLMIBGPTYV0@+O":
            return None
        fields.append((decode(raw_name).strip(), ftype, buf[pos + 16]))
        pos += 32
        if len(fields) > 255:
            return None
    if not fields or 1 + sum(f[2] for f in fields) != rec_len:
        return None
    return fields, n_rec, hdr_len, rec_len


def dbf_rows(buf, off, info, limit):
    fields, n_rec, hdr_len, rec_len = info
    out = []
    for i in range(min(n_rec, limit)):
        rec = buf[off + hdr_len + i * rec_len: off + hdr_len + (i + 1) * rec_len]
        p, row = 1, []
        for _, t, ln in fields:
            row.append(decode(rec[p:p + ln]).strip() if t not in "MGPB" else "<備忘>")
            p += ln
        out.append(row)
    return out


def report_dbf(name, buf, off, info, sample):
    fields, n_rec, _, _ = info
    print(f"  ▸ 資料表 {name}：{n_rec} 筆，{len(fields)} 個欄位")
    print("    欄位：" + "、".join(f"{f[0]}({f[1]}{f[2]})" for f in fields))
    if sample:
        for r in dbf_rows(buf, off, info, 3):
            print("    範例：" + " | ".join(r))


def scan_embedded(buf, sample, extract_dir=None):
    """格式不明時，在整個檔案裡找藏在裡面的 DBF 或 ZIP。"""
    found = 0
    i = 0
    while i < len(buf) - 32:
        info = parse_dbf_header(buf, i)
        if info:
            found += 1
            fields, n_rec, hdr_len, rec_len = info
            end = i + hdr_len + n_rec * rec_len
            # 往前找看看有沒有檔名（很多備份格式會把檔名放在資料前面）
            before = buf[max(0, i - 64):i]
            name_guess = ""
            for tok in before.split(b"\0")[::-1]:
                if b".DBF" in tok.upper() or b".dbf" in tok:
                    name_guess = decode(tok.strip(b"\x00\x01\x02 ")).strip()
                    break
            report_dbf(name_guess or f"#{found}（位移 {i}）", buf, i, info, sample)
            if extract_dir:
                fn = os.path.basename(name_guess) if name_guess.upper().endswith(".DBF") else f"table_{found}.dbf"
                with open(os.path.join(extract_dir, fn), "wb") as f:
                    f.write(buf[i:end])
            i = max(end, i + 1)
            continue
        i += 1
    return found


def inspect_bytes(name, buf, sample, extract_dir, depth=0):
    indent = "  " * depth
    kind = next((label for sig, label in SIGNATURES if buf.startswith(sig)), None)
    info = parse_dbf_header(buf)
    if info:
        print(f"{indent}{name}：dBASE / FoxPro 資料表（新系統可直接匯入）")
        report_dbf(name, buf, 0, info, sample)
        return
    if kind is None:
        print(f"{indent}{name}：不是常見格式，開頭位元組 {buf[:16].hex(' ')}")
        print(f"{indent}  正在掃描檔案內部是否包含資料表…")
        n = scan_embedded(buf, sample, extract_dir)
        if not n:
            print(f"{indent}  沒找到可辨識的資料表；可能是加密或專用壓縮格式。")
        return
    print(f"{indent}{name}：{kind}")
    if kind.startswith("ZIP"):
        with zipfile.ZipFile(io.BytesIO(buf)) as z:
            for zi in z.infolist():
                print(f"{indent}  - {zi.filename}（{zi.file_size:,} bytes）")
            for zi in z.infolist():
                if zi.is_dir() or zi.flag_bits & 0x1:
                    continue
                data = z.read(zi)
                if extract_dir:
                    target = os.path.join(extract_dir, os.path.basename(zi.filename))
                    with open(target, "wb") as f:
                        f.write(data)
                inspect_bytes(zi.filename, data, sample, None, depth + 1)
            if any(zi.flag_bits & 0x1 for zi in z.infolist()):
                print(f"{indent}  ⚠️ 有檔案設了密碼，需要密碼才能讀")
    elif kind.startswith("GZIP"):
        data = gzip.decompress(buf)
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                for m in t.getmembers():
                    if m.isfile():
                        inspect_bytes(m.name, t.extractfile(m).read(), sample, extract_dir, depth + 1)
        except tarfile.TarError:
            inspect_bytes(name + "（解壓後）", data, sample, extract_dir, depth + 1)
    else:
        print(f"{indent}  這個格式需要另外的工具解開，請把這份結果回傳，我再處理。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="備份檔（若有 .001 .002 … 分割檔，請全部列出，依序）")
    ap.add_argument("--sample", action="store_true", help="印出每個資料表前 3 筆（會包含資料內容）")
    ap.add_argument("--extract", metavar="資料夾", help="把找到的資料表另存成 .dbf 等檔案")
    args = ap.parse_args()
    if args.extract:
        os.makedirs(args.extract, exist_ok=True)
    buf = b"".join(open(f, "rb").read() for f in args.files)
    print(f"檔案：{', '.join(os.path.basename(f) for f in args.files)}，共 {len(buf):,} bytes")
    inspect_bytes(os.path.basename(args.files[0]), buf, args.sample, args.extract)
    if args.extract:
        print(f"\n已解出到：{os.path.abspath(args.extract)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
