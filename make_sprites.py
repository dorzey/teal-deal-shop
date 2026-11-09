#!/usr/bin/env python3
"""Generates public/ducks.svg: one <symbol> per species in 01_seed.sql, each a
stylised drake in side view, plus one shared `skeleton` symbol that X-ray mode
lays over the ducks. Species are matched by slug (lowercase, dashes), so the
storefront draws <use href="ducks.svg#northern-pintail">.

    python3 make_sprites.py
"""
import re

# name: (head, body, breast, wing, bill, opts)
# opts: eye, bill (n normal | s spoon | l long thin | h hooked | b bigknob), crest,
#       patch (face patch), cheek, collar, tail (long), cap, stripe, spec (wing
#       speculum), knob, flank (side patch), back
W, K = "#F7F5EE", "#1D1F24"
SPECIES = {
 # --- dabbling
 "Mallard": ("#1E7A4F", "#9A9487", "#7A4A30", "#8C8574", "#E8B820", dict(collar=W, spec="#2B4FA3")),
 "Northern Pintail": ("#6B4A36", "#A5A39B", W, "#8F8E86", "#5E6A74", dict(tail="long", stripe=W, spec="#2E7D5B")),
 "Green-winged Teal": ("#8A3B2E", "#9C9A93", "#D9B896", "#8A8880", "#3A3A3A", dict(patch="#1F7A4C", stripe="#F2E6B8", spec="#2E9B5A")),
 "Blue-winged Teal": ("#6D7F95", "#8A6B4E", "#C99262", "#7C6A58", "#33363B", dict(cheek=W, spec="#5BA3D9")),
 "Cinnamon Teal": ("#9B3A22", "#A0432A", "#A8472E", "#8C6D58", "#2B2B2B", dict(eye="#D63A2A", spec="#6FB4E6")),
 "Gadwall": ("#9B9488", "#8E8B83", "#B0ABA0", "#8A867D", "#4A4A4A", dict(back=K, spec=W)),
 "Eurasian Wigeon": ("#A5492B", "#9AA0A5", "#D9A98A", "#8E939A", "#7D8C9A", dict(cap="#F1DFA2", spec="#2E8E5B")),
 "American Wigeon": ("#A8A7A0", "#9F6F5A", "#BC8A72", "#8B6B5A", "#7D8C9A", dict(cap=W, patch="#2A8A57", spec="#2E8E5B")),
 "Northern Shoveler": ("#1F7A5A", "#8F5A34", W, "#7A8EA3", "#2E2E2E", dict(bill="s", flank="#8F5A34", eye="#E8C020", spec="#2E8E5B")),
 "American Black Duck": ("#8C8272", "#4A4038", "#5B4E43", "#40372F", "#C8C050", dict(spec="#4A5FB0")),
 "Garganey": ("#7A4A34", "#8F8B83", "#E4D7C0", "#7B8EA0", "#3A3A3A", dict(stripe=W, spec="#4F9A64")),
 "Falcated Duck": ("#6E3F59", "#9E9A92", "#A8A49C", "#6F7B88", "#2E2E2E", dict(crest=True, patch="#2D8E62", collar=W, tail="long")),
 "Mottled Duck": ("#B79E7C", "#7B6551", "#8A7058", "#6F5B49", "#D7B32E", dict(stripe="#4A3C30", spec="#4D6FC4")),
 "Gray Teal": ("#A89C88", "#9A8E7B", "#B3A793", "#8B8070", "#6E7782", dict(spec="#3E8A5C")),
 "Pacific Black Duck": ("#8C7B66", "#4B4036", "#5A4C40", "#3F362D", "#6F7780", dict(stripe=K, spec="#2E8A55")),
 "Cape Teal": ("#D9C9B5", "#C0B09B", "#CBBBA7", "#A99985", "#E07A8A", dict(spec="#2E8A55")),
 "Chestnut Teal": ("#1E7A55", "#8A3B22", "#9A4326", "#6E4B38", "#4A5560", dict(spec="#2E9B5A")),
 "Marbled Teal": ("#BDAE97", "#AA9B84", "#BDAE97", "#97887A", "#4A4A4A", dict(patch="#6F6154", crest=True)),
 "Laysan Duck": ("#6B4A3A", "#6A5240", "#7A5E4A", "#5A4636", "#E07A30", dict(cheek=W)),
 "Hawaiian Duck": ("#7A5E44", "#6D5440", "#7D6048", "#5B4636", "#B8A238", dict(spec="#4A7A6F")),
 "Yellow-billed Duck": ("#8F8A80", "#7E796F", "#9A958A", "#6E695F", "#F0CE20", dict(spec="#3E8A55")),
 "Spot-billed Duck": ("#9A7B5C", "#6F5A47", "#8E7358", "#5B4A3B", "#E3C020", dict(stripe=K, spec="#2F5FB5", knob="#D9402A")),
 # --- diving
 "Canvasback": ("#8A2E24", "#E6E4DC", K, "#CFCEC6", "#2B2B2B", dict(eye="#C63A2A")),
 "Redhead": ("#B0452C", "#9A9992", K, "#8A8984", "#7D93A8", dict(eye="#E3C020")),
 "Ring-necked Duck": ("#3A3A48", "#3C3C42", W, "#4A4A52", "#7D8C9A", dict(collar="#9A4A30", eye="#E3C020")),
 "Greater Scaup": ("#2A4A3A", "#E6E4DC", K, "#B5B3AC", "#6F9FD0", dict(eye="#E3C020")),
 "Lesser Scaup": ("#3A2A4A", "#E1DFD7", K, "#B0AEA8", "#7D9CC4", dict(eye="#E3C020")),
 "Tufted Duck": ("#26262E", "#1F1F24", W, "#2A2A30", "#7D8C9A", dict(crest=True, eye="#E3C020")),
 "Common Pochard": ("#A5452D", "#A5A39C", K, "#8F8D86", "#7D8C9A", dict(eye="#D63A2A")),
 "Ferruginous Duck": ("#8C3A1F", "#7E3A24", "#8C4630", "#6E3220", "#7D8C9A", dict(eye=W, spec=W)),
 "Southern Pochard": ("#2F2F36", "#3A3A42", "#444450", "#2E2E36", "#7D8C9A", dict(eye="#E3C020", cheek="#A0A0A8")),
 "Common Goldeneye": ("#1E5A4A", "#EAE8E0", "#EAE8E0", "#333338", "#2B2B2B", dict(eye="#F2D020", cheek=W)),
 "Barrow's Goldeneye": ("#3A2A5A", "#EAE8E0", "#EAE8E0", "#2E2E33", "#2B2B2B", dict(eye="#F2D020", cheek=W)),
 "Bufflehead": ("#3E2A6A", "#EDEBE4", "#EDEBE4", "#33343A", "#8A94A0", dict(cap=W)),
 "Hooded Merganser": ("#23232A", "#6A4A30", W, "#33343A", "#2B2B2B", dict(bill="l", crest=True, cap=W, eye="#E3C020")),
 "Common Merganser": ("#1F5A47", "#EFEDE6", "#F4DCC8", "#2E2E33", "#C8341F", dict(bill="l")),
 "Red-breasted Merganser": ("#1E4A3A", "#9A988F", "#B5683C", "#7A786F", "#C8341F", dict(bill="l", crest=True, collar=W, eye="#D63A2A")),
 "Smew": ("#EDEBE4", "#EDEBE4", "#EDEBE4", "#33343A", "#7D8C9A", dict(patch=K, crest=True, bill="n")),
 "Ruddy Duck": ("#26262E", "#A0442A", "#B5522F", "#8A3A24", "#4A8FD0", dict(cheek=W, tail="long")),
 # --- sea
 "Common Eider": ("#F2F0E8", "#F2F0E8", "#E8B8A4", K, "#B8C8A0", dict(cap=K, patch="#8AC0A0")),
 "King Eider": ("#8FB0C8", "#F2F0E8", "#F0C8A8", K, "#E87A2A", dict(bill="b", knob="#F0A030", cheek="#9ACDB4")),
 "Spectacled Eider": ("#8FC8A8", "#26262E", K, "#33343A", "#E8963C", dict(patch=W, eye="#222")),
 "Steller's Eider": ("#EDEBE4", "#9A6038", "#B5703E", K, "#7D8C9A", dict(patch="#7FB89A", cap="#8CBFA0", flank=K)),
 "Long-tailed Duck": ("#E8E6DE", "#E8E6DE", "#E8E6DE", K, "#2B2B2B", dict(tail="long", patch="#6A5A52", bill="n", collar=K)),
 "Harlequin Duck": ("#4A5F7A", "#5B6F8A", "#4F6580", "#46586E", "#5A6572", dict(patch=W, stripe="#B5522F", crest=False)),
 "Surf Scoter": ("#222226", "#222226", "#2A2A2E", "#1D1D21", "#E8963C", dict(cap=W, knob="#F5EFEF", bill="b")),
 "White-winged Scoter": ("#202024", "#202024", "#28282C", "#E8E6DE", "#E87A2A", dict(bill="b", knob="#222")),
 "Black Scoter": ("#1B1B1F", "#1B1B1F", "#232327", "#18181C", "#26262A", dict(bill="b", knob="#F09A20")),
 "Velvet Scoter": ("#1C1C20", "#1C1C20", "#24242A", "#E6E4DC", "#E87A2A", dict(bill="b", knob="#1F1F23", cheek=W)),
 "Common Scoter": ("#1B1B1F", "#1B1B1F", "#232327", "#18181C", "#26262A", dict(bill="b", knob="#F5B02A")),
 # --- perching
 "Wood Duck": ("#1E6A54", "#6B4A3A", "#8A3A3A", "#3E4A5A", "#E05A3A", dict(crest=True, stripe=W, eye="#D63A2A", collar=W)),
 "Mandarin Duck": ("#C25A2A", "#9A5A3A", "#6E4A8A", "#C8742A", "#E05A4A", dict(crest=True, cheek=W, stripe=W)),
 "Muscovy Duck": ("#26262E", "#2E2E3A", "#2E2E3A", W, "#D9B0A0", dict(knob="#C8341F", patch="#C8341F")),
 "Comb Duck": ("#F1F0EA", "#26262E", W, "#1E2A4A", "#2B2B2B", dict(knob="#26262E", bill="b")),
 "Maned Duck": ("#7A4A34", "#9A9990", "#C8A890", "#6F6F6A", "#4A4A4A", dict(crest=True, collar=K)),
 "Brazilian Teal": ("#6F5A44", "#9A7A5A", "#B08B68", "#5E4A3A", "#D9402A", dict(spec="#2E8E6A")),
 "Hartlaub's Duck": ("#2A2A30", "#3A3028", "#4A3E34", "#2C2C32", "#2B2B2B", dict(cap=W, back=K)),
 "Fulvous Whistling Duck": ("#C2864A", "#B5733A", "#D09556", "#4A3A2E", "#6E7782", dict(tail="long", collar=W)),
 "White-faced Whistling Duck": ("#222228", "#6A3E2A", K, "#2A2A30", "#6E7782", dict(patch=W, collar=K)),
 "Black-bellied Whistling Duck": ("#8E7A60", "#B5602E", K, W, "#E86A7A", dict(tail="long")),
}

def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def symbol(name, head, body, breast, wing, bill, o):
    out = []
    a = out.append
    eye = o.get("eye", K)
    # tail
    if o.get("tail"):
        a(f'<path d="M22 50 L2 38 L3 52 L8 56 L24 62 Z" fill="{body}"/>')
    else:
        a(f'<path d="M22 50 L8 44 L10 58 L24 62 Z" fill="{body}"/>')
    # body + breast + back + wing
    a(f'<ellipse cx="55" cy="56" rx="38" ry="22" fill="{body}"/>')
    if o.get("back"):
        a(f'<path d="M20 52 Q55 24 92 52 Q55 40 20 52Z" fill="{o["back"]}"/>')
    if o.get("flank"):
        a(f'<ellipse cx="62" cy="62" rx="16" ry="8" fill="{o["flank"]}"/>')
    a(f'<ellipse cx="82" cy="60" rx="14" ry="15" fill="{breast}"/>')
    a(f'<path d="M26 54 Q52 38 82 56 Q58 76 26 54Z" fill="{wing}"/>')
    if o.get("spec"):
        a(f'<path d="M30 55 Q50 50 66 56 Q50 62 30 55Z" fill="{o["spec"]}" opacity=".95"/>')
    # neck + head
    a(f'<path d="M78 56 Q84 40 84 30 L100 32 Q102 46 98 58 Z" fill="{head}"/>')
    if o.get("crest"):
        a(f'<path d="M84 26 Q70 14 62 22 Q72 22 78 30 Q70 32 66 40 Q80 38 88 34Z" fill="{head}"/>')
    a(f'<circle cx="91" cy="33" r="14" fill="{head}"/>')
    if o.get("collar"):
        a(f'<path d="M81 44 Q91 50 101 44 L101 48 Q91 54 81 48Z" fill="{o["collar"]}"/>')
    if o.get("cap"):
        a(f'<path d="M78 30 Q90 14 104 28 Q92 24 78 30Z" fill="{o["cap"]}"/>')
    if o.get("cheek"):
        a(f'<ellipse cx="94" cy="39" rx="7" ry="5" fill="{o["cheek"]}"/>')
    if o.get("patch"):
        a(f'<ellipse cx="94" cy="32" rx="8" ry="5.5" fill="{o["patch"]}"/>')
    if o.get("stripe"):
        a(f'<path d="M80 34 Q92 28 104 36 L104 38 Q92 32 80 37Z" fill="{o["stripe"]}"/>')
    # bill
    bt = o.get("bill", "n")
    bc = bill
    if bt == "s":
        a(f'<path d="M102 30 Q118 28 122 36 Q118 44 102 40Z" fill="{bc}"/>')
    elif bt == "l":
        a(f'<path d="M103 31 L124 34 L103 37Z" fill="{bc}"/><path d="M103 31 L103 37" stroke="{bc}"/>')
    elif bt == "b":
        a(f'<path d="M102 30 Q112 28 118 34 Q112 40 102 39Z" fill="{bc}"/>')
        a(f'<circle cx="104" cy="28" r="4.5" fill="{o.get("knob", bc)}"/>')
    else:
        a(f'<path d="M102 30 Q114 30 118 35 Q112 40 102 39Z" fill="{bc}"/>')
        if o.get("knob"):
            a(f'<circle cx="104" cy="29" r="3" fill="{o["knob"]}"/>')
    # eye
    a(f'<circle cx="94" cy="30" r="2.6" fill="{eye}"/><circle cx="94.7" cy="29.2" r=".8" fill="{W}"/>')
    # waterline
    a('<path d="M4 78 Q18 72 32 78 T60 78 T88 78 T116 78" fill="none" stroke="#4FA5C0" stroke-width="3" stroke-linecap="round" opacity=".55"/>')
    return f'<symbol id="{slug(name)}" viewBox="0 0 124 90">' + "".join(out) + "</symbol>"

def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return tuple(u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
                 for a, b, c, d in zip(p0, p1, p2, p3))

def skeleton_symbol():
    """One skeleton shared by every species — they share a body plan (see symbol())
    — drawn in the same 124x90 frame, in currentColor so CSS can make it glow. It is
    only shown in the storefront's X-ray mode, laid over the inverted duck."""
    out = []
    a = out.append
    neck = ((90, 43), (94, 50), (86, 56), (76, 46))      # skull base -> shoulders (an S)
    back = ((76, 46), (60, 37), (40, 40), (24, 52))      # shoulders -> tail
    # vertebral column: a line, then a bead per vertebra
    for curve, n, r in ((neck, 8, 1.7), (back, 10, 2.0)):
        pts = [_bezier(*curve, i / n) for i in range(n + 1)]
        a('<path d="M%.1f %.1f C%d %d %d %d %d %d" stroke-width="1"/>' % (*pts[0], *curve[1], *curve[2], *curve[3]))
        for x, y in pts[1:-1] if curve is neck else pts[1:]:
            a(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="currentColor" fill-opacity=".85" stroke-width=".6"/>')
    a('<path d="M24 52 L10 46 L12 55 Z" fill="currentColor" fill-opacity=".25"/>')            # pygostyle
    # ribs: from the back, curving down and forward to the keel
    for t in (.20, .32, .44, .56, .68):
        x, y = _bezier(*back, t)
        a('<path d="M%.1f %.1f Q%.1f %.1f %.1f %.1f"/>' % (x, y, x + 15, y + 5, x + 8, min(74, y + 31)))
    a('<path d="M44 74 Q66 80 90 67" stroke-width="2.4"/>')                                   # keel
    a('<path d="M80 49 Q83 64 93 58"/>')                                                      # wishbone
    # wing: humerus, radius + ulna, hand
    a('<path d="M66 47 L48 56" stroke-width="2.2"/><path d="M48 56 L27 57.5"/><path d="M48 53.5 L27 55"/>'
      '<path d="M27 57.5 L19 61.5"/>')
    # pelvis, leg, webbed foot
    a('<path d="M36 52 Q42 62 52 58"/><path d="M36 52 L28 54"/>')
    a('<path d="M44 60 L54 69" stroke-width="2.2"/><path d="M54 69 L46 82" stroke-width="2.2"/>')
    a('<path d="M46 82 L38 87 M46 82 L47 88 M46 82 L55 86 M38 87 Q47 90 55 86" stroke-width="1"/>')
    # skull, eye socket, bill
    a('<path d="M82 34 Q83 24 92 24 Q102 25 102 33 Q101 41 92 42 Q83 42 82 34Z" fill="currentColor" fill-opacity=".14"/>')
    a('<circle cx="94" cy="30" r="3.8" fill="#000" fill-opacity=".55"/>')
    a('<path d="M101 31 Q111 31 117 35 Q111 39 101 38"/><path d="M103 34.5 L115 35"/>')
    return ('<symbol id="skeleton" viewBox="0 0 124 90"><g fill="none" stroke="currentColor" stroke-width="1.3" '
            'stroke-linecap="round" stroke-linejoin="round">' + "".join(out) + "</g></symbol>")

if __name__ == "__main__":
    assert len(SPECIES) == 60, len(SPECIES)
    body = "\n".join(symbol(n, *v[:5], v[5]) for n, v in SPECIES.items())
    with open("public/ducks.svg", "w") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n{body}\n{skeleton_symbol()}\n</svg>\n')
    print(f"wrote public/ducks.svg ({len(SPECIES)} symbols + skeleton)")
