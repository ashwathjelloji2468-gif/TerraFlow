# Breach equations for M2

## Read this first

- **Originals not available.** The only source in hand is the Azmi preprint (Research Square,
  doi:10.21203/rs.3.rs-7289241/v1). Every base equation below (§1–3, §5) is transcribed from
  **Azmi's reproduction** in Tables 1–3, *not* from the original paper. Each is tagged
  `SECONDARY (Azmi p.N, Table T)`.
- Because of that, **original page/equation numbers and original valid ranges are NOT AVAILABLE**.
  Azmi's own tables contain labelling errors (§6), so treat every base coefficient as
  unverified until someone checks it against the original.
- The DFM fusion equations (§4) are **PRIMARY**: Azmi is their original source.
- `UNCLEAR` = the PDF could not be read cleanly; the best reading is given with the reason.
  `NOT STATED` = Azmi doesn't give it.
- Page numbers are the preprint's "Page N/22" footer. Azmi numbers tables, not equations.

## 0. Symbols and units (Azmi p.4)

| Symbol | Meaning | Unit |
|---|---|---|
| Q_p | breach peak discharge | m³/s |
| B_ave | final breach average width, the mean of top and bottom widths measured along the crest | m |
| T_f | failure time ("breach formation time"): onset of formation to full completion | h |
| V_w | volume of water above breach invert | m³ |
| h_w | height of water above breach invert | m |
| h_b | breach height (dam top minus bottom elevation at the breach) | m |
| h_d | dam height | m |
| W_ave | average embankment width | m |
| S | reservoir storage (only for equations not used here) | m³ |
| g | gravitational acceleration: **NOT STATED**; SI units imply 9.81 m/s² | m/s² |
| h_r | Xu & Zhang model reference height: **15.0** (fixed model constant) | m |

Categorical inputs (Azmi p.4): dam type **HD** homogeneous, **CD** core wall, **FD** concrete-faced,
**ZD** zoned-fill; failure mode **O** overtopping, **P** piping; erodibility **H / M / L**.

Several base equations are empirical and not dimensionally homogeneous. **Use SI inputs (m, m³)
exactly as listed.**

---

## 1. Peak discharge Q_p [m³/s]

### 1.1 Froehlich (2016b) — code F16

- Source: `SECONDARY (Azmi p.6, Table 1, footnote a)`. Original: Froehlich DC (2016b), Predicting
  peak discharge from gradually breached embankment dam, J Hydrol Eng 21(11),
  doi:10.1061/(asce)he.1943-5584.0001424. Original page/eq: NOT AVAILABLE.

```
Q_p = 0.0175 · k_m · k_h · ( g · h_w · V_w · h_b² / W_ave )^0.5
```

| Factor | Condition | Value |
|---|---|---|
| k_m | overtopping (O) | 1.85 |
| k_m | piping (P) | 1 |
| k_h | h_b ≤ 6.1 m | 1 |
| k_h | h_b > 6.1 m | (h_b / 6.1)^(1/8) |

- Units check: (m/s² · m · m³ · m² / m)^0.5 = m³/s ✓. The equation writes k_m, k_h; the footnote writes K_m, K_h (same factors).
- Valid range: NOT AVAILABLE.

### 1.2 Xu & Zhang (2009) — code XZ9

- Source: `SECONDARY (Azmi p.6, Table 1, footnote b)`. Original: Xu Y, Zhang LM (2009), Breaching
  parameters for earth and rockfill dams, J Geotech Geoenviron Eng 135(12):1957–1970,
  doi:10.1061/(asce)gt.1943-5606.0000162. Original page/eq: NOT AVAILABLE.

```
Q_p = 0.175 · √g · V_w^(5/6) · (h_d / h_r)^0.199 · (V_w^(1/3) / h_w)^(−1.274) · e^(B4)
B4  = b3 + b4 + b5
```

| Term | Class | Value |
|---|---|---|
| b3 (dam type) | core wall (CD) | −0.503 |
| b3 | concrete-faced (FD) | −0.591 |
| b3 | homogeneous / zoned-fill (HD/ZD) | −0.649 |
| b4 (failure mode) | overtopping (O) | −0.705 |
| b4 | piping (P) | −1.039 |
| b5 (erodibility) | high (H) | −0.007 |
| b5 | medium (M) | −0.375 |
| b5 | low (L) | −1.362 |

- Units check: √g · V_w^(5/6) = m^0.5/s · m^2.5 = m³/s ✓. The remaining factors are dimensionless.
- `h_r` is the fixed Xu & Zhang model constant, 15.0 m. Source: PRIMARY (Xu & Zhang 2009),
  doi:10.1061/(asce)gt.1943-5606.0000162.
- Valid range: NOT AVAILABLE.
- **Implementation status (2026-10-01, Feature 3):** implemented in `backend/m2_breach/xz9.py`
  `peak_discharge_xz9` exactly as transcribed above, with g = 9.81 m/s² (§0, as F16/F8);
  `verified=False` — not checked against the original paper (docs/decisions.md 2026-10-01).

### 1.3 Zhong et al. (2020) — code Z20

- Source: `SECONDARY (Azmi p.6, Table 1)`. Original: Zhong Q, Chen S, Fu Z, Shan Y (2020), New
  empirical model for breaching of earth-rock dams, Nat Hazards Rev 21(2),
  doi:10.1061/(asce)nh.1527-6996.0000374. Original page/eq: NOT AVAILABLE.

```
Q_p = √g · h_w^(−0.5) · V_w · F

HD:  F = (V_w^0.333 / h_w)^(−1.58) · (h_w / h_b)^(−0.76) · h_d^0.1   · e^(−4.55)
CD:  F = (V_w^0.333 / h_w)^(−1.51) · (h_w / h_b)^(−1.09) · h_d^(−0.12) · e^(−3.61)
```

- **UNCLEAR (typesetting):** both bracketed ratios print as overlapping stacked glyphs with no
  fraction bar. Read at 600 dpi as V_w^0.333 over h_w and h_w over h_b; the reverse ratio can't be
  ruled out.
- **UNCLEAR:** the √ bar covers only g as printed, so the reading is √g · h_w^(−0.5) · V_w. That
  gives m³/s ✓, which supports it.
- Only **HD** and **CD** branches are given. Azmi doesn't state how FD or ZD dams map onto them
  (**NOT STATED**), nor whether Zhong's "HD/CD" mean the same as Azmi's codes.
- h_d^0.1 and h_d^(−0.12) are dimensional, so h_d must be in metres.
- Valid range: NOT AVAILABLE.

---

## 2. Average breach width B_ave [m]

### 2.1 Froehlich (1995) — code F95

- Source: `SECONDARY (Azmi p.8, Table 3, footnote b)`. Original: Froehlich DC (1995), Embankment
  dam breach parameters revisited, Water Resources Engineering (ASCE). Original page/eq: NOT AVAILABLE.

```
B_ave = 0.1803 · K_n · V_w^0.32 · h_b^0.19
```

| Factor | Condition | Value |
|---|---|---|
| K_n | overtopping (O) | 1.4 |
| K_n | piping (P) | 1 |

- Valid range: NOT AVAILABLE.

### 2.2 Froehlich (2008) — code F8

- Source: `SECONDARY (Azmi p.8, Table 3, footnote a)`. Original: Froehlich DC (2008), Embankment
  dam breach parameters and their uncertainties, J Hydraul Eng. Original page/eq: NOT AVAILABLE.

```
B_ave = 0.27 · K_O · V_w^0.32 · h_b^0.04
```

| Factor | Condition | Value |
|---|---|---|
| K_O | overtopping (O) | 1.3 |
| K_O | piping (P) | 1 |

- **UNCLEAR (label):** the equation writes K_O but footnote a calls the factor K_m. The values are
  read as belonging to K_O, since the footnote is attached to this row.
- Valid range: NOT AVAILABLE.

### 2.3 Xu & Zhang (2009) — code XZ9

- Source: `SECONDARY (Azmi p.8, Table 3, footnote c)`. Original: as in §1.2.

```
B_ave = 0.787 · h_b · (h_d / h_r)^0.133 · (V_w^0.333 / h_w)^0.652 · e^(B3)
B3    = b3 + b4 + b5
```

| Term | Class | Value |
|---|---|---|
| b3 (dam type) | core walls | −0.041 |
| b3 | concrete-faced | 0.026 |
| b3 | homogeneous / zoned-fill (HD/ZD) | −0.226 |
| b4 (failure mode) | overtopping (O) | 0.149 |
| b4 | piping (P) | −0.389 |
| b5 (erodibility) | high (HE) | 0.291 |
| b5 | medium (ME) | −0.14 |
| b5 | low (LE) | −0.391 |

- **UNCLEAR (label):** footnote c prints "concrete-faced dams (CD)", which conflicts with Table 1
  (CD = core wall, FD = concrete-faced). The table maps by the *words*: core walls → CD,
  concrete-faced → FD.
- The 0.333 exponent is printed offset above V_w, but it clearly belongs to V_w.
- `h_r = 15.0 m` is a fixed Xu & Zhang model constant, not a site input.
- Source: PRIMARY (Xu & Zhang 2009), doi:10.1061/(asce)gt.1943-5606.0000162.
- Valid range: NOT AVAILABLE.

---

## 3. Failure time T_f [h]

Definition used by Azmi (p.4, p.15): from the onset of breach formation to full completion. The
Xu & Zhang (2009) T_f equation is **excluded** because it covers the whole erosion period.

### 3.1 Froehlich (1995) — code F95

- Source: `SECONDARY (Azmi p.7, Table 2)`. Original: as in §2.1.

```
T_f = 0.00254 · V_w^0.53 · h_b^(−0.9)          [h]
```

- The exponents are printed offset above their symbols, but which exponent belongs to which symbol is clear.
- Output unit: **h, implied.** Azmi records T_f in hours and applies no conversion here, but it
  doesn't state the unit for this equation.
- Valid range: NOT AVAILABLE.

### 3.2 Froehlich (2008) — code F8

- Source: `SECONDARY (Azmi p.7, Table 2)`. Original: as in §2.2.

```
T_f = 63.2 · ( V_w / (g · h_b²) )^0.5 / 3600   [h]    (63.2·√(…) is in seconds)
```

- **UNCLEAR (layout):** the exponent "2" is printed detached above the h_b term. h_b² is the only
  reading that makes the square root come out in seconds, which the /3600 → hours conversion requires.
- Valid range: NOT AVAILABLE.

### 3.3 MacDonald & Langridge-Monopolis (1984) — code MCLM

- Source: `SECONDARY (Azmi p.7, Table 2)`. Original: **not in Azmi's reference list** — citation
  NOT AVAILABLE. Original page/eq: NOT AVAILABLE.

```
T_f = 0.0179 · ( 0.0261 · (V_w · h_w)^0.769 )^0.364     [h]
```

- Azmi gives this single form and doesn't define the inner bracketed term (NOT STATED). Whether
  the original has variants for different dam materials is also NOT STATED here.
- Output unit: h, implied (as in §3.1).
- Valid range: NOT AVAILABLE.

---

## 4. Updated DFM fusion equations — PRIMARY (Azmi p.11, Table 5)

Each code is the output of the base equation above, in the output unit (Q_p m³/s, B_ave m,
T_f h). The fit is a no-intercept linear model; coefficients are the median of 100,000 bootstrap
fits (random 80/20 split, Levenberg–Marquardt, relative error threshold 1e−2).

```
Q_p   = 0.3048·F16 + 0.4804·XZ9 + 0.1674·Z20        [m³/s]
B_ave = −0.8220·F95 + 1.0021·F8 + 1.1031·XZ9        [m]
T_f   = −1.0648·F95 + 1.5875·F8 + 0.6189·MCLM       [h]
```

| Output | Term | Median | median ± MAD |
|---|---|---|---|
| Q_p | a · F16 | 0.3048 | (−0.22, 0.82) |
| Q_p | b · XZ9 | 0.4804 | (0.17, 0.77) |
| Q_p | c · Z20 | 0.1674 | (−0.42, 0.74) |
| B_ave | a · F95 | −0.8220 | (−1.10, −0.54) |
| B_ave | b · F8 | 1.0020 / 1.0021 — **UNCLEAR** | (0.68, 1.32) |
| B_ave | c · XZ9 | 1.1031 | (0.95, 1.26) |
| T_f | a · F95 | −1.0648 | (−2.10, −0.03) |
| T_f | b · F8 | 1.5875 | (0.58, 2.59) |
| T_f | c · MCLM | 0.6189 | (0.39, 0.84) |

- **UNCLEAR:** Table 5 gives B_ave b as 1.0020 in the coefficient column and 1.0021 in the
  equation column. The difference is 0.01% of F8, so either value works; record which one you use.
- Stated validity (p.2, p.3, p.16): man-made **earthfill/rockfill embankment dams**. The equations
  are not recommended for **concrete dams**, or for embankment dams with extensive safety elements
  (wave walls, additional rock-mesh protection).
- Calibration subsets (p.4): Q_p 54 cases (those with W_ave), B_ave 130, T_f 65.
- Numeric input ranges: **NOT AVAILABLE.** Azmi refers to an Appendix A holding the database, but it
  is not in this PDF.
- Gap-filling baked into the fit (p.3–4): missing h_d → h_d = h_b; missing erodibility → M; a
  reported range → its midpoint.
- Negative coefficients can make B_ave or T_f ≤ 0 for unusual inputs. Check the sign and flag it
  (our rule).

## 5. DFM 2024 — needed for the recommended Q_p pair

The recommended Q_p pair is Updated DFM + DFM 2024 (Azmi p.15), so M2 also needs:

```
Q_p (DFM 2024) = 1.23·F16 − 0.84·H14 + 0.26·XZ9          [m³/s]
Q_p (H14)      = 0.0212 · V_w^0.5429 · h_w^0.8713       [m³/s]
```

- DFM 2024: `SECONDARY (Azmi p.7, Table 1 continued, footnote d)`. Original: Azmi M, Thomson K
  (2024), Nat Hazards 120:4423–4461.
- H14: `SECONDARY (Azmi p.6, Table 1)`. Original (Hooshyaripor et al. 2014) is not in Azmi's
  reference list, so its citation is NOT AVAILABLE.
- Valid range: NOT AVAILABLE.

The other pairs need nothing extra: B_ave is Updated DFM + XZ9 (§2.3), and T_f is Updated DFM +
F8 (§3.2).

## 6. Known problems in Azmi's tables

1. Dam-type labels: Table 1 footnote b has CD = core walls, FD = concrete-faced; Table 3 footnote c
   prints "concrete-faced dams (CD)".
2. F8 width factor: the equation says K_O, the footnote says K_m.
3. B_ave DFM coefficient b: 1.0020 vs 1.0021.
4. Z20: the stacked ratios are broken in typesetting (p.6, also in the Table 2/3 Z20 rows not used here).
5. Displaced superscripts in F8 T_f, F95 T_f and XZ9 B_ave.
6. g and h_r are never defined.
7. MCLM and H14 are used but missing from the reference list.

## 7. M2 implementation checklist

- [ ] One function per base equation. Put its `SECONDARY` tag in the docstring and return
      `verified=False` until it has been checked against the original.
- [ ] Z20: raise for dam types other than HD/CD until the mapping is sourced (Teesta III is FD).
- [ ] Unit tests: dimension checks for Q_p (F16, XZ9, Z20); continuity of k_h at h_b = 6.1 m;
      F8 T_f in seconds ÷ 3600; each DFM equation equals its weighted sum.
- [ ] Output all pair members plus the code, branch and coefficients used. If any input has
      `status: placeholder`, mark the output as a placeholder too.
- [ ] Refuse `kind: concrete_dam`; warn on `kind: moraine_dammed_lake` (outside calibration).
