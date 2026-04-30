#!/usr/bin/env python3
"""Extract Boss_Ogre_55515 row from characterinfo.pabgb"""
from __future__ import annotations
import argparse, csv, os, sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.vfs_manager import VfsManager
from core.pabgb_parser import parse_pabgb

def find_ogre_row(vfs):
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except:
            continue
        pabgb_entry, pabgh_entry = None, None
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            if pl == 'gamedata/characterinfo.pabgb': pabgb_entry = entry
            elif pl == 'gamedata/characterinfo.pabgh': pabgh_entry = entry
        
        if not pabgb_entry or not pabgh_entry: continue
        try:
            d = vfs.read_entry_data(pabgb_entry)
            hd = vfs.read_entry_data(pabgh_entry)
            t = parse_pabgb(d, hd, 'characterinfo.pabgb')
            for r in t.rows:
                if r.name == 'Boss_Ogre_55515': return r, t.file_name
        except Exception as e:
            print('Error:', e, file=sys.stderr)
    return None

def find_siblings(vfs, count=2):
    results = []
    for group in vfs.list_package_groups():
        try:
            pamt = vfs.load_pamt(group)
        except: continue
        pabgb_entry, pabgh_entry = None, None
        for entry in pamt.file_entries:
            pl = entry.path.lower()
            if pl == 'gamedata/characterinfo.pabgb': pabgb_entry = entry
            elif pl == 'gamedata/characterinfo.pabgh': pabgh_entry = entry
        
        if not pabgb_entry or not pabgh_entry: continue
        try:
            d = vfs.read_entry_data(pabgb_entry)
            hd = vfs.read_entry_data(pabgh_entry)
            t = parse_pabgb(d, hd, 'characterinfo.pabgb')
            for r in t.rows:
                if r.name and r.name.startswith('Boss_') and r.name != 'Boss_Ogre_55515':
                    results.append((r, t.file_name))
                    if len(results) >= count: return results
        except: pass
    return results

def dump_csv(row, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['field_idx', 'offset', 'size', 'kind', 'raw_hex', 'value_u32', 'value_f32', 'value_str'])
        for i, fld in enumerate(row.fields):
            v_u32 = v_f32 = v_str = ''
            if fld.kind == 'u32': v_u32 = str(fld.value) if isinstance(fld.value, int) else ''
            elif fld.kind == 'f32': v_f32 = f"{float(fld.value):.6f}" if isinstance(fld.value, (int, float)) else ''
            elif fld.kind == 'str': v_str = str(fld.value)
            elif fld.kind == 'hash': v_u32 = f"0x{fld.value:08X}" if isinstance(fld.value, int) else ''
            w.writerow([i, fld.offset, fld.size, fld.kind, fld.raw.hex(), v_u32, v_f32, v_str])

def dump_txt(row, path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"=== {row.display_name} ===\n")
        f.write(f"Index: {row.index}\n")
        f.write(f"Hash: 0x{row.row_hash:08X}\n")
        f.write(f"Size: {row.data_size} bytes\n")
        f.write(f"Fields: {len(row.fields)}\n\n")
        
        tc = {}
        for fld in row.fields: tc[fld.kind] = tc.get(fld.kind, 0) + 1
        for k in sorted(tc): f.write(f"{k}: {tc[k]}\n")
        f.write("\n")
        
        for i, fld in enumerate(row.fields):
            if fld.kind == 'str':
                f.write(f"[{i}] {fld.kind} @ {fld.offset}: {fld.value}\n")
            elif fld.kind == 'f32':
                f.write(f"[{i}] {fld.kind} @ {fld.offset}: {float(fld.value):.6f}\n")
            else:
                f.write(f"[{i}] {fld.kind} @ {fld.offset}: {fld.value}\n")

def safe_filename(s):
    return s.replace('_', '_').replace(' ', '_').replace('/', '_')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--game', default=r'C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert')
    p.add_argument('--out', default=str(Path(__file__).parent))
    args = p.parse_args()
    
    print('Loading VFS...')
    vfs = VfsManager(args.game)
    
    result = find_ogre_row(vfs)
    if not result:
        print('ERROR: Boss_Ogre_55515 not found', file=sys.stderr)
        return 1
    
    row, fname = result
    print(f'Found: {row.display_name}')
    
    os.makedirs(args.out, exist_ok=True)
    csv_p = os.path.join(args.out, 'ogre_row_dump.csv')
    txt_p = os.path.join(args.out, 'ogre_row_dump.txt')
    
    dump_csv(row, csv_p)
    dump_txt(row, txt_p)
    
    tc = {}
    for fld in row.fields: tc[fld.kind] = tc.get(fld.kind, 0) + 1
    
    print(f'Row Size: {row.data_size} bytes')
    print(f'Field Count: {len(row.fields)}')
    print(f'Type Breakdown: {dict(sorted(tc.items()))}')
    
    print('\nDumping sibling bosses...')
    sibs = find_siblings(vfs, 2)
    for i, (sr, _) in enumerate(sibs):
        sc = {}
        for fld in sr.fields: sc[fld.kind] = sc.get(fld.kind, 0) + 1
        
        safe_name = safe_filename(sr.display_name)
        csv_s = os.path.join(args.out, f'sibling_{i}_{safe_name}_row_dump.csv')
        txt_s = os.path.join(args.out, f'sibling_{i}_{safe_name}_row_dump.txt')
        
        dump_csv(sr, csv_s)
        dump_txt(sr, txt_s)
        
        print(f'  [{i}] {sr.display_name}: {sr.data_size}b, {len(sr.fields)} fields')

if __name__ == '__main__':
    sys.exit(main())
