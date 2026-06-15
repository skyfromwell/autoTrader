#!/usr/bin/env python3
from __future__ import annotations
"""
Post-TP regime classifier.
Takes 8 market structure features, returns (regime, confidence).
Regimes: CONTINUATION, CONSOLIDATION, REVERSAL, MIXED
"""


class RegimeClassifier:
    def classify(self, features: dict, pair: str = "",
                 direction: str = "") -> tuple[str, float]:
        slope     = features.get("pivot_slope",  0)    or 0
        atr_ratio = features.get("atr_ratio",  1.0)   or 1.0
        # F1-F4 are Lorentzian-normalized 0-1 (ml.n_rsi / ml.n_adx / etc.)
        # Neutral midpoint is 0.50; >0.50 = bullish bias, <0.50 = bearish bias
        rsi       = features.get("f1_rsi14",  0.50)  or 0.50
        adx       = features.get("f4_adx",   0.25)   or 0.25
        wt        = features.get("f2_wt",    0.50)   or 0.50
        cci       = features.get("f3_cci",   0.50)   or 0.50
        kernel_ok = features.get("kernel_dir", False)

        is_long = direction == "long"
        score   = 0.40   # baseline

        # Slope alignment with direction
        if is_long  and slope > 0.10: score += 0.25
        elif not is_long and slope < -0.10: score += 0.25

        # ATR trend (raw ratio — unchanged)
        if   atr_ratio >= 0.85: score += 0.15
        elif atr_ratio <  0.75: score -= 0.15

        # RSI momentum (normalized: 0.50 = neutral)
        if is_long  and rsi > 0.50: score += 0.15
        elif not is_long and rsi < 0.50: score += 0.15

        # ADX strength (normalized: 0.50 ≈ strong, 0.25 ≈ moderate, 0.15 ≈ weak)
        if   adx > 0.50: score += 0.20
        elif adx > 0.30: score += 0.10
        elif adx < 0.15: score -= 0.10

        # Kernel direction
        if kernel_ok: score += 0.15

        # WaveTrend + CCI confirmation (normalized: >0.50 = bullish)
        if (wt  > 0.50) == is_long: score += 0.05
        if (cci > 0.50) == is_long: score += 0.05

        score = max(0.0, min(1.0, score))

        # Consolidation: ATR contracting regardless of direction
        if atr_ratio < 0.75:
            return "CONSOLIDATION", min(score, 0.70)

        # Reversal: slope against direction
        if is_long  and slope < -0.10:
            return "REVERSAL", min(0.90, 1.0 - score + 0.50)
        if not is_long and slope > 0.10:
            return "REVERSAL", min(0.90, 1.0 - score + 0.50)

        if score >= 0.60:
            return "CONTINUATION", min(score, 0.90)
        if score <= 0.40:
            return "MIXED", 0.50

        return "MIXED", score
