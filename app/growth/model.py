"""Bayesian linear regression over the tested factors, with Thompson sampling on top.

Every video changes several things at once, so comparing averages per arm would credit one
factor with another's effect. Fitting them together separates them. The trend term stops
a growing channel from making whatever was tried last look best.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import numpy as np

_INTERCEPT, _TREND = "intercept", "trend"
# Per 30 days, in log views. A new channel's first weeks can really move by this much.
_PRIOR_VARIANCE = {_INTERCEPT: 100.0, _TREND: 25.0}
# One unit of reward is roughly e times the views, so most arms should sit well inside this.
_ARM_PRIOR_VARIANCE = 1.0
_NOISE_PRIOR_STRENGTH, _NOISE_PRIOR = 4.0, 1.0
_HALF_LIFE_DAYS = 45.0
_TREND_SCALE_DAYS = 30.0
_NOISE_ITERATIONS = 5
_MAX_LOG_EFFECT = 20.0


@dataclass
class Observation:
    arms: dict[str, str]
    reward: float
    day: float  # days since the channel's first observation


def _feature(factor: str, arm: str) -> str:
    return f"{factor}={arm}"


class Model:
    def __init__(self, observations: Sequence[Observation]) -> None:
        self.observations = [
            o for o in observations
            if isinstance(o.reward, (int, float)) and isinstance(o.day, (int, float))
            and math.isfinite(o.reward) and math.isfinite(o.day)
        ]
        self.counts: dict[str, dict[str, int]] = {}
        for obs in self.observations:
            for factor, arm in obs.arms.items():
                arms = self.counts.setdefault(factor, {})
                arms[arm] = arms.get(arm, 0) + 1
        names = [_INTERCEPT, _TREND] + sorted(
            _feature(f, a) for f, arms in self.counts.items() for a in arms
        )
        self._index = {name: i for i, name in enumerate(names)}
        self._mean = np.zeros(len(names))
        self._prior_vars = [_PRIOR_VARIANCE.get(n, _ARM_PRIOR_VARIANCE) for n in names]
        self._cov = np.diag(self._prior_vars)
        self.noise = _NOISE_PRIOR
        if self.observations:
            self._fit()

    def _fit(self) -> None:
        X = np.zeros((len(self.observations), len(self._index)))
        y = np.array([o.reward for o in self.observations], dtype=float)
        days = np.array([o.day for o in self.observations], dtype=float)
        latest = float(days.max())
        # Audiences drift, so older results count for less.
        weights = 0.5 ** (np.maximum(0.0, latest - days) / _HALF_LIFE_DAYS)
        for row, obs in enumerate(self.observations):
            X[row, self._index[_INTERCEPT]] = 1.0
            X[row, self._index[_TREND]] = obs.day / _TREND_SCALE_DAYS
            for factor, arm in obs.arms.items():
                X[row, self._index[_feature(factor, arm)]] = 1.0
        prior_precision = np.diag([1.0 / v for v in self._prior_vars])
        XtW = X.T * weights
        XtX = XtW @ X
        Xty = XtW @ y
        w_sum = float(weights.sum())
        for _ in range(_NOISE_ITERATIONS):  # the noise estimate depends on the fit, and the fit on the noise
            precision = prior_precision + XtX / self.noise
            precision = (precision + precision.T) / 2.0
            self._cov = np.linalg.pinv(precision, hermitian=True)
            self._mean = self._cov @ Xty / self.noise
            residual = y - X @ self._mean
            # Residuals alone look small when there are nearly as many parameters as videos.
            unfitted = np.maximum(0.0, ((X @ self._cov) * X).sum(axis=1))
            self.noise = max(
                0.05,
                (_NOISE_PRIOR_STRENGTH * _NOISE_PRIOR + float(weights @ (residual**2 + unfitted)))
                / (_NOISE_PRIOR_STRENGTH + w_sum),
            )
        self._cov = (self._cov + self._cov.T) / 2.0

    def draw(self, rng: Optional[np.random.Generator | int] = None) -> "Draw":
        gen = rng if isinstance(rng, np.random.Generator) else np.random.default_rng(rng)
        return Draw(self._index, gen.multivariate_normal(self._mean, self._cov, method="svd"))

    def summary(self, draws: int = 2000, rng: Optional[np.random.Generator] = None) -> dict:
        """Per factor: each tested arm's multiplier on whatever was fitted, against the factor's average, and P(best)."""
        draws = max(1, int(draws))
        rng = rng or np.random.default_rng(0)
        samples = rng.multivariate_normal(self._mean, self._cov, size=draws, method="svd")
        out: dict[str, list[dict]] = {}
        for factor, arms in self.counts.items():
            if len(arms) < 2:
                continue
            names = sorted(arms)
            columns = samples[:, [self._index[_feature(factor, a)] for a in names]]
            effects = np.clip(columns - columns.mean(axis=1, keepdims=True), -_MAX_LOG_EFFECT, _MAX_LOG_EFFECT)
            best = np.bincount(columns.argmax(axis=1), minlength=len(names)) / draws
            low, high = np.percentile(effects, [5, 95], axis=0)
            out[factor] = sorted(
                ({"arm": a, "n": arms[a], "multiplier": round(float(np.exp(effects[:, i].mean())), 2),
                  "low": round(float(np.exp(low[i])), 2), "high": round(float(np.exp(high[i])), 2),
                  "p_best": round(float(best[i]), 3)} for i, a in enumerate(names)),
                key=lambda row: -row["p_best"],
            )
        return out


class Draw:
    """One plausible version of the truth. Acting on it is what Thompson sampling is."""

    def __init__(self, index: dict[str, int], values: np.ndarray) -> None:
        self._index, self._values = index, values

    def arm(self, factor: str, arm: str) -> float:
        # An arm never tried has a zero prior mean, so give it a prior-sized draw of its own.
        position = self._index.get(_feature(factor, arm))
        if position is not None:
            return float(self._values[position])
        seed = zlib.crc32(f"{factor}={arm}|{self._values[0]}".encode())
        return float(np.random.default_rng(seed).normal(0.0, _ARM_PRIOR_VARIANCE**0.5))

    def score(self, arms: dict[str, str], factors: Iterable[str]) -> float:
        return sum(self.arm(f, arms[f]) for f in factors if f in arms)
