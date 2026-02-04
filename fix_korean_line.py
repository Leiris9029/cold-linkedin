import json
import re
import sys
import io

# Force UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

with open(r'c:\Users\Lei\Desktop\JAPAN\output\ja_dm2_sections.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# First 20 entries in the array
entries_to_fix = data[:20]

results = []

for entry in entries_to_fix:
    idx = entry['index']
    name = entry['name']
    text = entry['dm2_text']

    # Split into variant A and variant B sections
    # The B header can be: ### B안, ### B (Softer, etc.
    b_pattern = r'(### B[^\n]*\n)'
    parts = re.split(b_pattern, text, maxsplit=1)

    if len(parts) == 3:
        variant_a = parts[0]
        b_header = parts[1]
        variant_b_body = parts[2]
    else:
        print(f"WARNING: Could not split index {idx} ({name})")
        results.append({"index": idx, "name": name, "new_dm2_text": text})
        continue

    def fix_variant(variant_text):
        """Fix the Korean line placement in a single variant."""
        # Find the Korean line (contains 韓国)
        lines = variant_text.split('\n')

        korean_line_idx = None
        korean_line = None
        for i, line in enumerate(lines):
            if '韓国' in line and line.strip():
                korean_line_idx = i
                korean_line = line.strip()
                break

        if korean_line_idx is None:
            return variant_text  # No Korean line found, skip

        # Remove the Korean line from its current position
        lines_without_korean = []
        for i, line in enumerate(lines):
            if i == korean_line_idx:
                continue
            lines_without_korean.append(line)

        # Work with the cleaned text (without Korean line)
        cleaned_text = '\n'.join(lines_without_korean)

        # Clean up any triple+ blank lines that resulted from removal
        while '\n\n\n' in cleaned_text:
            cleaned_text = cleaned_text.replace('\n\n\n', '\n\n')

        # Split into lines again
        lines = cleaned_text.split('\n')

        # Find the CTA line (contains 2月16 or meeting request)
        cta_line_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and '2月16' in stripped:
                cta_line_idx = i
                break

        if cta_line_idx is None:
            # Fallback: look for もしご関心 or もしよろしければ
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped and ('もしご関心' in stripped or 'もしよろしければ' in stripped):
                    cta_line_idx = i
                    break

        if cta_line_idx is None:
            print(f"  WARNING: Could not find CTA line in variant")
            return variant_text  # Can't find CTA, return original

        # The product description paragraph ends just before the blank line(s) before CTA
        # Go backwards from CTA to find the last non-blank line before it
        product_end_idx = cta_line_idx - 1
        while product_end_idx >= 0 and not lines[product_end_idx].strip():
            product_end_idx -= 1

        if product_end_idx < 0:
            return variant_text

        # Build new text: insert Korean line right after product_end_idx
        new_lines = []
        for i, line in enumerate(lines):
            new_lines.append(line)
            if i == product_end_idx:
                new_lines.append(korean_line)

        result = '\n'.join(new_lines)
        return result

    # Fix variant A
    fixed_a = fix_variant(variant_a)

    # Fix variant B (b_header + b_body)
    full_b = b_header + variant_b_body
    fixed_b = fix_variant(full_b)

    new_text = fixed_a + fixed_b

    results.append({
        "index": idx,
        "name": name,
        "new_dm2_text": new_text
    })

# Write results
with open(r'c:\Users\Lei\Desktop\JAPAN\output\ja_fix_batch1.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

# Print diagnostics - check correct placement
print("=== DIAGNOSTICS: Korean line placement after fix ===\n")
for r in results:
    idx = r['index']
    name = r['name']
    text = r['new_dm2_text']

    lines = text.split('\n')
    korean_count = 0
    for i, line in enumerate(lines):
        if '韓国' in line and line.strip():
            korean_count += 1
            prev_line = lines[i-1].strip() if i > 0 else '<START>'
            next_line = lines[i+1].strip() if i < len(lines)-1 else '<END>'

            # Check: prev line should be non-empty (product desc), next should be empty (blank line before CTA)
            prev_ok = bool(prev_line) and '韓国' not in (lines[i-1] if i > 0 else '')
            next_ok = next_line == ''

            status = "OK" if (prev_ok and next_ok) else "NEEDS CHECK"

            print(f"[{status}] Index {idx} ({name}) Korean occurrence #{korean_count}:")
            print(f"  PREV: ...{prev_line[-60:] if len(prev_line) > 60 else prev_line}")
            print(f"  KOR:  {line.strip()[:80]}...")
            print(f"  NEXT: '{next_line[:60]}'")
            print()

print(f"\nTotal entries processed: {len(results)}")
print("Done!")
