"""Bayesian linear regression over the tested factors, with Thompson sampling on top.

Every video changes several things at once, so comparing averages per arm would credit one
factor with another's effect. Fitting them together separates them. The trend term stops
a growing channel from making whatever was tried last look best.
"""
from __future__ import annotations

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


@dataclass
class Observation:
    arms: dict[str, str]
    reward: float
    day: float  # days since the channel's first observation


def _feature(factor: str, arm: str) -> str:
    return f"{factor}={arm}"


class Model:
    def __init__(self, observations: Sequence[Observation]) -> None:
        self.observations = list(observations)
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
        self._cov = np.diag([_PRIOR_VARIANCE.get(n, _ARM_PRIOR_VARIANCE) for n in names])
        self.noise = _NOISE_PRIOR
        if self.observations:
            self._fit()

    def _fit(self) -> None:
        X = np.zeros((len(self.observations), len(self._index)))
        y = np.array([o.reward for o in self.observations], dtype=float)
        latest = max(o.day for o in self.observations)
        # Audiences drift, so older results count for less.
        weights = np.array([0.5 ** ((latest - o.day) / _HALF_LIFE_DAYS) for o in self.observations])
        for row, obs in enumerate(self.observations):
            X[row, self._index[_INTERCEPT]] = 1.0
            X[row, self._index[_TREND]] = obs.day / _TREND_SCALE_DAYS
            for factor, arm in obs.arms.items():
                X[row, self._index[_feature(factor, arm)]] = 1.0
        prior_precision = np.linalg.inv(self._cov)
        XtW = X.T * weights
        for _ in range(_NOISE_ITERATIONS):  # the noise estimate depends on the fit, and the fit on the noise
            precision = prior_precision + XtW @ X / self.noise
            self._cov = np.linalg.inv(precision)
            self._mean = self._cov @ (XtW @ y) / self.noise
            residual = y - X @ self._mean
            # Residuals alone look small when there are nearly as many parameters as videos.
            unfitted = ((X @ self._cov) * X).sum(axis=1)
            self.noise = max(0.05, (_NOISE_PRIOR_STRENGTH * _NOISE_PRIOR + float(weights @ (residual**2 + unfitted)))
                             / (_NOISE_PRIOR_STRENGTH + float(weights.sum())))
        self._cov = (self._cov + self._cov.T) / 2

    def draw(self, rng: np.random.Generator) -> "Draw":
        return Draw(self._index, rng.multivariate_normal(self._mean, self._cov, method="svd"))

    def summary(self, draws: int = 2000, rng: Optional[np.random.Generator] = None) -> dict:
        """Per factor: each tested arm's multiplier on whatever was fitted, against the factor's average, and P(best)."""
        rng = rng or np.random.default_rng(0)
        samples = rng.multivariate_normal(self._mean, self._cov, size=draws, method="svd")
        out: dict[str, list[dict]] = {}
        for factor, arms in self.counts.items():
            if len(arms) < 2:
                continue
            names = sorted(arms)
            columns = samples[:, [self._index[_feature(factor, a)] for a in names]]
            effects = columns - columns.mean(axis=1, keepdims=True)
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
