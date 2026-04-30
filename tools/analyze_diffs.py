"""Cross-correlate two diff CSVs (e.g. diff_1_2 and diff_3_4) to find
addresses that BEHAVE LIKE HP -- decreased in BOTH boss fights at the
same memory address. False positives (animations / particle systems)
should differ between the two fights; the real boss-HP cell will be
present in both diffs as a positive-fp32 decrease.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path


def load_decreased(csv_path: Path) -> dict[str, tuple[float, float]]:
    """Return {addr_hex: (old_f32, new_f32)} for fp32 decreases in
    HP-shape range."""
    out = {}
    with open(csv_path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                of_ = float(row["old_f32"])
                nf  = float(row["new_f32"])
            except Exception:
                continue
            # HP-shape: positive fp32, decreased
            if not (1 <= of_ <= 1_000_000):
                continue
            if not (0 <= nf < of_):
                continue
            out[row["addr_hex"]] = (of_, nf)
    return out


def load_decreased_i32(csv_path: Path) -> dict[str, tuple[int, int]]:
    out = {}
    with open(csv_path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                o = int(row["old_i32"])
                n = int(row["new_i32"])
            except Exception:
                continue
            if not (1 <= o <= 1_000_000):
                continue
            if not (0 <= n < o):
                continue
            out[row["addr_hex"]] = (o, n)
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: analyze_diffs.py <diff_A_B.csv> <diff_C_D.csv>")
        return 1

    a_path = Path(sys.argv[1])
    b_path = Path(sys.argv[2])
    if not a_path.exists() or not b_path.exists():
        print("missing diff CSV(s)")
        return 1

    print(f"loading {a_path.name} (fp32 decreases) ...")
    a_fp = load_decreased(a_path)
    print(f"  {len(a_fp):,} fp32 decreases")
    print(f"loading {b_path.name} (fp32 decreases) ...")
    b_fp = load_decreased(b_path)
    print(f"  {len(b_fp):,} fp32 decreases")

    # Addresses present in BOTH diffs == addresses that consistently
    # decrease across two independent boss fights.
    common = set(a_fp.keys()) & set(b_fp.keys())
    print(f"\n=== fp32 addresses that decreased in BOTH fights: {len(common):,} ===")

    # For each common address, sort by total damage delta (sum of
    # both fights). The HP cell should consistently lose value across
    # both fights.
    rows = []
    for addr in common:
        a_o, a_n = a_fp[addr]
        b_o, b_n = b_fp[addr]
        rows.append((addr, a_o, a_n, b_o, b_n,
                     (a_o - a_n) + (b_o - b_n)))
    rows.sort(key=lambda r: -r[5])

    print(f"\n{'addr':<20s} {'A_old':>10s} {'A_new':>10s} "
          f"{'B_old':>10s} {'B_new':>10s} {'sum_delta':>10s}")
    print("-" * 75)
    for addr, ao, an, bo, bn, total in rows[:60]:
        print(f"{addr:<20s} {ao:>10.2f} {an:>10.2f} "
              f"{bo:>10.2f} {bn:>10.2f} {total:>10.2f}")

    # Also: addresses where BOTH old values are similar (same boss spawn HP)
    print(f"\n=== Same-old-value candidates (boss spawned with same HP both times) ===")
    same_old = [(addr, ao, an, bo, bn, total)
                for (addr, ao, an, bo, bn, total) in rows
                if abs(ao - bo) < 1.0]
    print(f"{len(same_old)} addresses where A_old ~= B_old (within 1.0)")
    for addr, ao, an, bo, bn, total in same_old[:40]:
        print(f"  {addr}  starts ~{ao:.1f}  ends A={an:.1f} B={bn:.1f}  total dmg {total:.1f}")

    # i32
    print(f"\n=== i32 cross-check ===")
    a_i = load_decreased_i32(a_path)
    b_i = load_decreased_i32(b_path)
    common_i = set(a_i.keys()) & set(b_i.keys())
    print(f"i32 addresses decreased in both: {len(common_i):,}")
    rows_i = []
    for addr in common_i:
        a_o, a_n = a_i[addr]
        b_o, b_n = b_i[addr]
        rows_i.append((addr, a_o, a_n, b_o, b_n,
                       (a_o - a_n) + (b_o - b_n)))
    rows_i.sort(key=lambda r: -r[5])
    print(f"\nTop 30 i32 cross-fight decreasers:")
    for addr, ao, an, bo, bn, total in rows_i[:30]:
        print(f"  {addr}  A:{ao}->{an}  B:{bo}->{bn}  sum_delta {total}")

    # Same-old i32
    same_old_i = [(addr, ao, an, bo, bn, total)
                  for (addr, ao, an, bo, bn, total) in rows_i
                  if ao == bo]
    print(f"\n{len(same_old_i)} i32 addresses where A_old == B_old "
          f"(strong 'spawn HP' signal):")
    for addr, ao, an, bo, bn, total in same_old_i[:30]:
        print(f"  {addr}  starts {ao}  ends A={an} B={bn}  total dmg {total}")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
