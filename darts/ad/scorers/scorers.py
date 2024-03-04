"""
Scorers Base Classes
"""

# TODO:
#     - add stride for Scorers like Kmeans and Wasserstein
#     - add option to normalize the windows for kmeans? capture only the form and not the values.


import copy
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Sequence, Union

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from darts import TimeSeries
from darts.ad.utils import (
    _assert_same_length,
    _assert_timeseries,
    _intersect,
    _sanity_check_two_series,
    eval_metric_from_scores,
    show_anomalies_from_scores,
)
from darts.logging import get_logger, raise_if_not
from darts.utils.utils import _build_tqdm_iterator, _parallel_apply, series2seq

logger = get_logger(__name__)


class AnomalyScorer(ABC):
    """Base class for all anomaly scorers"""

    def __init__(
        self,
        univariate_scorer: bool,
        fittable: bool,
        single_series_support: bool,
        probabilistic_support: bool,
        window_agg: Optional[bool],
        window: int = 1,
        diff_fn: str = "abs_diff",
        n_jobs: int = 1,
    ) -> None:
        """
        Args:
            univariate_scorer (bool): If set to `True`, the scorer will output a univariate series
                as anomaly scores, even if the input series is multivariate. If set to `False`, the
                returned scores will preserse the dimensionality of the input time series.
            window (int): If greater then 1, anomaly scores will be derived/combined so that each timestamp
                t of the returned score series will be derived from the window [t-W, t] (considering a context
                window instead of  single point). The returned scores will be `N - W + 1`. Defaults to 1.
            fittable (bool): Whether the anomaly scorer should be fitted before being applied.
            single_series_support (bool): Whether the anomaly scorer supports processing a single series besides
                pairs of predictions and actual anomalies. If set to `False`, methods like eval_metric, score,
                show_anomalies, will not be available, and the `*_from_prediction` methods should be used
                instead.
            probabilistic_support (bool): Whether the model expects the prediction series to be probabilistic
                series.
            window_agg (bool): Transforms a window-wise anomaly score into a point-wise anomaly score.
                When using a window of size `W`, a scorer will return an anomaly score
                with values that represent how anomalous each past `W` is. If the parameter
                `window_agg` is set to True (default value), the scores for each point
                can be assigned by aggregating the anomaly scores for each window the point
                is included in.

                This post-processing step is equivalent to a rolling average of length window
                over the anomaly score series. The return anomaly score represents the abnormality
                of each timestamp.
            diff_fn (str, optional): Function used to reduce `actual_series` and `pred_series` to a single
                series in case `single_series_support` is set to `True` and `*_from_prediction` methods
                are called. Defaults to "abs_diff".
            n_jobs (int, optional): Number of processes that will be instantiated for tasks that can be
                parallelized. Defaults to 1.
        """

        raise_if_not(
            type(window) is int,
            f"Parameter `window` must be an integer, found type {type(window)}.",
        )

        raise_if_not(
            window > 0,
            f"Parameter `window` must be stricly greater than 0, found size {window}.",
        )

        self.window = window
        self.univariate_scorer = univariate_scorer
        self.fittable = fittable
        self._fit_called = None
        self._is_probabilistic = probabilistic_support

        if fittable:
            # indicates if the scorer has been trained yet
            self._fit_called = False

        # function used in ._diff_series() to convert 2 time series into 1
        if diff_fn in {"abs_diff", "diff"}:
            self.diff_fn = diff_fn
        else:
            raise ValueError(f"Metric should be 'diff' or 'abs_diff', found {diff_fn}")

        raise_if_not(
            type(window_agg) is bool,
            f"Parameter `window_agg` must be Boolean, found type: {type(window_agg)}.",
        )

        self.window_agg = window_agg
        self.single_series_support = single_series_support
        self._n_jobs = n_jobs

    def _check_univariate_scorer(self, actual_anomalies: Sequence[TimeSeries]):
        """Checks if `actual_anomalies` contains only univariate series when the scorer has the
        parameter 'univariate_scorer' set to True.

        'univariate_scorer' is:
            True -> when the function of the scorer ``score(series)`` (or, if applicable,
                ``score_from_prediction(actual_series, pred_series)``) returns a univariate
                anomaly score regardless of the input `series` (or, if applicable, `actual_series`
                and `pred_series`).
            False -> when the scorer will return a series that has the
                same number of components as the input (can be univariate or multivariate).
        """

        if self.univariate_scorer:
            raise_if_not(
                all([isinstance(s, TimeSeries) for s in actual_anomalies]),
                "all series in `actual_anomalies` must be of type TimeSeries.",
            )

            raise_if_not(
                all([s.width == 1 for s in actual_anomalies]),
                f"Scorer {self.__str__()} will return a univariate anomaly score series (width=1)."
                + " Found a multivariate `actual_anomalies`."
                + " The evaluation of the accuracy cannot be computed between the two series.",
            )

    def _check_window_size(self, series: TimeSeries):
        """Checks if the parameter window is less or equal than the length of the given series"""

        raise_if_not(
            self.window <= len(series),
            f"Window size {self.window} is greater than the targeted series length {len(series)}, "
            + "must be lower or equal. Decrease the window size or increase the length series input"
            + " to score on.",
        )

    @property
    def is_probabilistic(self) -> bool:
        """Whether the scorer expects a probabilistic prediction for its first input."""
        return self._is_probabilistic

    def _assert_stochastic(self, series: TimeSeries, name_series: str):
        "Checks if the series is stochastic (number of samples is higher than one)."

        raise_if_not(
            series.is_stochastic,
            f"Scorer {self.__str__()} is expecting `{name_series}` to be a stochastic timeseries"
            + f" (number of samples must be higher than 1, found: {series.n_samples}).",
        )

    def _extract_deterministic(
        self, series: TimeSeries, name_series: str
    ) -> TimeSeries:
        """Checks if the series is deterministic (number of samples is equal to one).
        If so, returns the input series. If not, extracts the 0.5 quantile from the
        0.5 quantile from the stochastic time series.

        Parameters
        ----------
            series (TimeSeries): Input time series to be checked.
            name_series (str): Name of the series, used for detailed logging in case a
                stochastic timesries is passed.

        Returns
        -------
            TimeSeries: Original time series if `series` is deterministic.
                0.5 quantile of the stochastic time series if `series` is stochastic.
        """

        if not series.is_deterministic:
            logger.warning(
                f"Scorer {self.__str__()} is expecting `{name_series}` to be a (sequence of) deterministic"
                + f" timeseries (number of samples must be equal to 1, found: {series.n_samples}). The "
                + "series will be converted to a deterministic series by taking the median of the samples.",
            )
            series = series.quantile_timeseries(quantile=0.5)

        return series

    @abstractmethod
    def __str__(self):
        """returns the name of the scorer"""
        pass

    def _fun_window_agg(
        self, list_scores: Sequence[TimeSeries], window: int
    ) -> Sequence[TimeSeries]:
        """
        Transforms a window-wise anomaly score into a point-wise anomaly score.

        When using a window of size `W`, a scorer will return an anomaly score
        with values that represent how anomalous each past `W` is. If the parameter
        `window_agg` is set to True (default value), the scores for each point
        can be assigned by aggregating the anomaly scores for each window the point
        is included in.

        This post-processing step is equivalent to a rolling average of length window
        over the anomaly score series. The return anomaly score represents the abnormality
        of each timestamp.
        """
        list_scores_point_wise = []
        for score in list_scores:
            mean_score = np.empty(score.all_values().shape)
            for idx_point in range(len(score)):
                # "look ahead window" to account for the "look behind window" of the scorer
                mean_score[idx_point] = score.all_values(copy=False)[
                    idx_point : idx_point + window
                ].mean(axis=0)

            score_point_wise = TimeSeries.from_times_and_values(
                score.time_index, mean_score, columns=score.components
            )

            list_scores_point_wise.append(score_point_wise)

        return list_scores_point_wise

    def check_if_fit_called(self):
        """Checks if the scorer has been fitted before calling its `score()` function."""

        raise_if_not(
            self._fit_called,
            f"The Scorer {self.__str__()} has not been fitted yet. Call ``fit()`` first.",
        )

    def _check_single_series_support(func):
        """Decorator checking whether the scorer supports processing single series. If not,
        the `*_from_prediction` methods should be used instead.
        """

        def wrapper(self: "AnomalyScorer", *args, **kwargs):
            raise_if_not(
                self.single_series_support,
                f"Cannot call {func.__name__} when `single_series_support` is set to `False`. Please"
                "use the `*_from_prediction` methods instead.",
            )
            return func(self, *args, **kwargs)

        return wrapper

    def _check_probabilistic_series(
        self, pred_series: Sequence[TimeSeries]
    ) -> Sequence[TimeSeries]:
        """Checking whether the `pred_series`:
        - Is a stochastic series in case `is_probabilistic` is set to `True`, raises an exception
        otherwise
        - Extract a deterministic series from the stochastic series in case `pred_series` is stochastic
        but the model expects a deterministic series.
        """

        new_series = []
        for series in pred_series:
            if self.is_probabilistic:
                self._assert_stochastic(series, "pred_series")
                new_series.append(series)
            else:
                new_series.append(self._extract_deterministic(series, "pred_series"))

        return new_series

    @_check_single_series_support
    def eval_metric(
        self,
        actual_anomalies: Union[TimeSeries, Sequence[TimeSeries]],
        series: Union[TimeSeries, Sequence[TimeSeries]],
        metric: str = "AUC_ROC",
    ) -> Union[float, Sequence[float], Sequence[Sequence[float]]]:
        """Computes the anomaly score of the given time series, and returns the score
        of an agnostic threshold metric.

        Parameters
        ----------
        actual_anomalies
            The ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        series
            The (sequence of) series to detect anomalies from.
        metric
            Optionally, metric function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"

        Returns
        -------
        Union[float, Sequence[float], Sequence[Sequence[float]]]
            Score of an agnostic threshold metric for the computed anomaly score
                - ``float`` if `series` is a univariate series (dimension=1).
                - ``Sequence[float]``

                    * if `series` is a multivariate series (dimension>1), returns one
                    value per dimension, or
                    * if `series` is a sequence of univariate series, returns one value
                    per series
                - ``Sequence[Sequence[float]]]`` if `series` is a sequence of multivariate
                series. Outer Sequence is over the sequence input and the inner Sequence
                is over the dimensions of each element in the sequence input.
        """
        actual_anomalies = series2seq(actual_anomalies)
        self._check_univariate_scorer(actual_anomalies)
        anomaly_score = self.score(series)

        if self.window_agg:
            window = 1
        else:
            window = self.window

        return eval_metric_from_scores(actual_anomalies, anomaly_score, window, metric)

    def eval_metric_from_prediction(
        self,
        actual_anomalies: Union[TimeSeries, Sequence[TimeSeries]],
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
        metric: str = "AUC_ROC",
    ) -> Union[float, Sequence[float], Sequence[Sequence[float]]]:
        """Computes the anomaly score between `actual_series` and `pred_series`, and returns the score
        of an agnostic threshold metric.

        Parameters
        ----------
        actual_anomalies
            The (sequence of) ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        actual_series
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.
        metric
            Optionally, metric function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"

        Returns
        -------
        Union[float, Sequence[float], Sequence[Sequence[float]]]
            Score of an agnostic threshold metric for the computed anomaly score
                - ``float`` if `actual_series` and `actual_series` are univariate series (dimension=1).
                - ``Sequence[float]``

                    * if `actual_series` and `actual_series` are multivariate series (dimension>1),
                    returns one value per dimension, or
                    * if `actual_series` and `actual_series` are sequences of univariate series,
                    returns one value per series
                - ``Sequence[Sequence[float]]]`` if `actual_series` and `actual_series` are sequences
                of multivariate series. Outer Sequence is over the sequence input and the inner
                Sequence is over the dimensions of each element in the sequence input.
        """
        actual_anomalies = series2seq(actual_anomalies)
        self._check_univariate_scorer(actual_anomalies)

        anomaly_score = self.score_from_prediction(actual_series, pred_series)

        return eval_metric_from_scores(
            actual_anomalies, anomaly_score, self.window, metric
        )

    @_check_single_series_support
    def show_anomalies(
        self,
        series: TimeSeries,
        actual_anomalies: TimeSeries = None,
        scorer_name: str = None,
        title: str = None,
        metric: str = None,
    ):
        """Plot the results of the scorer.

        Computes the score on the given series input. And plots the results.

        The plot will be composed of the following:
            - the series itself.
            - the anomaly score of the score.
            - the actual anomalies, if given.

        It is possible to:
            - add a title to the figure with the parameter `title`
            - give personalized name to the scorer with `scorer_name`
            - show the results of a metric for the anomaly score (AUC_ROC or AUC_PR),
            if the actual anomalies is provided.

        Parameters
        ----------
        series
            The series to visualize anomalies from.
        actual_anomalies
            The ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        scorer_name
            Name of the scorer.
        title
            Title of the figure
        metric
            Optionally, Scoring function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"
        """

        if isinstance(series, Sequence):
            raise_if_not(
                len(series) == 1,
                "``show_anomalies`` expects one series for `series`,"
                + f" found a list of length {len(series)} as input.",
            )

            series = series[0]

        raise_if_not(
            isinstance(series, TimeSeries),
            "``show_anomalies`` expects an input of type TimeSeries,"
            + f" found type {type(series)} for `series`.",
        )

        anomaly_score = self.score(series)

        if title is None:
            title = f"Anomaly results by scorer {self.__str__()}"

        if scorer_name is None:
            scorer_name = f"anomaly score by {self.__str__()}"

        if self.window_agg:
            window = 1
        else:
            window = self.window

        return show_anomalies_from_scores(
            series,
            anomaly_scores=anomaly_score,
            window=window,
            names_of_scorers=scorer_name,
            actual_anomalies=actual_anomalies,
            title=title,
            metric=metric,
        )

    def show_anomalies_from_prediction(
        self,
        actual_series: TimeSeries,
        pred_series: TimeSeries,
        scorer_name: str = None,
        actual_anomalies: TimeSeries = None,
        title: str = None,
        metric: str = None,
    ):
        """Plot the results of the scorer.

        Computes the anomaly score on the two series. And plots the results.

        The plot will be composed of the following:
            - the actual_series and the pred_series.
            - the anomaly score of the scorer.
            - the actual anomalies, if given.

        It is possible to:
            - add a title to the figure with the parameter `title`
            - give personalized name to the scorer with `scorer_name`
            - show the results of a metric for the anomaly score (AUC_ROC or AUC_PR),
              if the actual anomalies is provided.

        Parameters
        ----------
        actual_series
            The actual series to visualize anomalies from.
        pred_series
            The predicted series of `actual_series`.
        actual_anomalies
            The ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        scorer_name
            Name of the scorer.
        title
            Title of the figure
        metric
            Optionally, Scoring function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"
        """
        if isinstance(actual_series, Sequence):
            raise_if_not(
                len(actual_series) == 1,
                "``show_anomalies_from_prediction`` expects only one series for `actual_series`,"
                + f" found a list of length {len(actual_series)} as input.",
            )

            actual_series = actual_series[0]

        raise_if_not(
            isinstance(actual_series, TimeSeries),
            "``show_anomalies_from_prediction`` expects an input of type TimeSeries,"
            + f" found type {type(actual_series)} for `actual_series`.",
        )

        if isinstance(pred_series, Sequence):
            raise_if_not(
                len(pred_series) == 1,
                "``show_anomalies_from_prediction`` expects one series for `pred_series`,"
                + f" found a list of length {len(pred_series)} as input.",
            )

            pred_series = pred_series[0]

        raise_if_not(
            isinstance(pred_series, TimeSeries),
            "``show_anomalies_from_prediction`` expects an input of type TimeSeries,"
            + f" found type: {type(pred_series)} for `pred_series`.",
        )

        anomaly_score = self.score_from_prediction(actual_series, pred_series)

        if title is None:
            title = f"Anomaly results by scorer {self.__str__()}"

        if scorer_name is None:
            scorer_name = [f"anomaly score by {self.__str__()}"]

        return show_anomalies_from_scores(
            actual_series,
            model_output=pred_series,
            anomaly_scores=anomaly_score,
            window=self.window,
            names_of_scorers=scorer_name,
            actual_anomalies=actual_anomalies,
            title=title,
            metric=metric,
        )

    def _score_core(self, series: TimeSeries) -> TimeSeries:
        raise NotImplementedError()

    @_check_single_series_support
    def score(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Computes the anomaly score on the given series.

        If a sequence of series is given, the scorer will score each series independently
        and return an anomaly score for each series in the sequence.

        Parameters
        ----------
        series
            The (sequence of) series to detect anomalies from.

        Returns
        -------
        Union[TimeSeries, Sequence[TimeSeries]]
            (Sequence of) anomaly score time series
        """

        self.check_if_fit_called()

        list_series = series2seq(series)

        for s in list_series:
            _assert_timeseries(s)
            self._check_window_size(s)

        list_series = [self._extract_deterministic(s, "series") for s in list_series]

        anomaly_scores = [self._score_core(series) for series in list_series]

        if len(anomaly_scores) == 1 and not isinstance(series, Sequence):
            return anomaly_scores[0]
        else:
            return anomaly_scores

    def _score_core_from_prediction(
        self, actual_series: TimeSeries, pred_series: TimeSeries
    ) -> TimeSeries:
        # does not need to be implemented in case there is no single series support,
        # thus not making it abstract
        raise NotImplementedError()

    def score_from_prediction(
        self,
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Computes the anomaly score on the two (sequence of) series.

        The function ``diff_fn`` passed as a parameter to the scorer, will transform `pred_series` and `actual_series`
        into one "difference" series. By default, ``diff_fn`` will compute the absolute difference
        (Default: "abs_diff").
        If actual_series and pred_series are sequences, ``diff_fn`` will be applied to all pairwise elements
        of the sequences.

        The scorer will then transform this series into an anomaly score. If a sequence of series is given,
        the scorer will score each series independently and return an anomaly score for each series in the sequence.

        Parameters
        ----------
        actual_series
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.

        Returns
        -------
        Union[TimeSeries, Sequence[TimeSeries]]
            (Sequence of) anomaly score time series
        """

        if self.fittable:
            self.check_if_fit_called()

        def _check_ts_or_list_ts(input):
            if not isinstance(actual_series, list):
                input = [input]
            for series in input:
                _assert_timeseries(series)

        _check_ts_or_list_ts(actual_series)
        _check_ts_or_list_ts(pred_series)

        list_actual_series, list_pred_series = series2seq(actual_series), series2seq(
            pred_series
        )
        # checking for probabilistic series or extracting a deterministic one
        list_actual_series = [
            self._extract_deterministic(s, "actual_series") for s in list_actual_series
        ]

        list_pred_series = self._check_probabilistic_series(list_pred_series)

        _assert_same_length(list_actual_series, list_pred_series)

        anomaly_scores = []

        for s1, s2 in zip(list_actual_series, list_pred_series):
            _sanity_check_two_series(s1, s2)
            s1, s2 = _intersect(s1, s2)
            self._check_window_size(s1)
            self._check_window_size(s2)

            # if single series is supported, then we reduce the two series to one, and call _score_core
            if self.single_series_support:
                single_series = self._diff_series(s1, s2)
                anomaly_scores.append(self._score_core(single_series))
            # otherwise, we expect _score_core_from_prediction to be implemented, and we call that one
            else:
                anomaly_scores.append(self._score_core_from_prediction(s1, s2))

        if (
            len(anomaly_scores) == 1
            and not isinstance(pred_series, Sequence)
            and not isinstance(actual_series, Sequence)
        ):
            return anomaly_scores[0]
        else:
            return anomaly_scores

    def _fit_core(self, series: List[TimeSeries]) -> Any:
        raise NotImplementedError

    @_check_single_series_support
    def fit(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
    ):
        """Fits the scorer on the given time series input.

        If sequence of series is given, the scorer will be fitted on the concatenation of the sequence.

        The assumption is that the series `series` used for training are generally anomaly-free.

        Parameters
        ----------
        series
            The (sequence of) series with no anomalies.

        Returns
        -------
        self
            Fitted Scorer.
        """
        list_series = series2seq(series)

        for idx, s in enumerate(list_series):
            _assert_timeseries(s)

            if idx == 0:
                self.width_trained_on = s.width
            else:
                raise_if_not(
                    s.width == self.width_trained_on,
                    "series in `series` must have the same number of components,"
                    + f" found number of components equal to {self.width_trained_on}"
                    + f" at index 0 and {s.width} at index {idx}.",
                )
            self._check_window_size(s)

            s = self._extract_deterministic(s, "series")

        self._fit_core(list_series)
        self._fit_called = True

    def fit_from_prediction(
        self,
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
    ):
        """Fits the scorer on the two (sequence of) series.

        The function ``diff_fn`` passed as a parameter to the scorer, will transform `pred_series` and `actual_series`
        into one series. By default, ``diff_fn`` will compute the absolute difference (Default: "abs_diff").
        If `pred_series` and `actual_series` are sequences, ``diff_fn`` will be applied to all pairwise elements
        of the sequences.

        The scorer will then be fitted on this (sequence of) series. If a sequence of series is given,
        the scorer will be fitted on the concatenation of the sequence.

        The scorer assumes that the (sequence of) actual_series is anomaly-free.

        Parameters
        ----------
        actual_series
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.

        Returns
        -------
        self
            Fitted Scorer.
        """
        list_actual_series, list_pred_series = series2seq(actual_series), series2seq(
            pred_series
        )

        list_actual_series = self._check_probabilistic_series(list_actual_series)
        list_pred_series = self._check_probabilistic_series(list_pred_series)

        _assert_same_length(list_actual_series, list_pred_series)

        list_fit_series = []
        for s1, s2 in zip(list_actual_series, list_pred_series):
            _sanity_check_two_series(s1, s2)
            s1 = self._extract_deterministic(s1, "actual_series")
            s2 = self._extract_deterministic(s2, "pred_series")
            list_fit_series.append(self._diff_series(s1, s2))

        self.fit(list_fit_series)

    def _diff_series(self, series_1: TimeSeries, series_2: TimeSeries) -> TimeSeries:
        """Applies the ``diff_fn`` to the two time series. Converts two time series into 1.

        series_1 and series_2 must:
            - have a non empty time intersection
            - be of the same width W

        Parameters
        ----------
        series_1
            1st time series
        series_2:
            2nd time series

        Returns
        -------
        TimeSeries
            series of width W
        """
        series_1, series_2 = _intersect(series_1, series_2)

        if self.diff_fn == "abs_diff":
            return (series_1 - series_2).map(lambda x: np.abs(x))
        elif self.diff_fn == "diff":
            return series_1 - series_2
        else:
            # found an non-existent diff_fn
            raise ValueError(
                f"Metric should be 'diff' or 'abs_diff', found {self.diff_fn}"
            )


class FittableAnomalyScorer(AnomalyScorer):
    def __init__(
        self,
        univariate_scorer: bool,
        single_series_support: bool,
        probabilistic_support: bool,
        window_agg: Optional[bool] = None,
        window: int = 1,
        diff_fn: str = "abs_diff",
        n_jobs: int = 1,
    ) -> None:
        super().__init__(
            univariate_scorer=univariate_scorer,
            fittable=True,
            single_series_support=single_series_support,
            probabilistic_support=probabilistic_support,
            window_agg=window_agg,
            window=window,
            diff_fn=diff_fn,
            n_jobs=n_jobs,
        )


class WindowedAnomalyScorer(FittableAnomalyScorer):
    """Base class for anomaly scorers that rely on windows to detect anomalies"""

    def _tabularize_series(
        self, list_series: Sequence[TimeSeries], concatenate: bool, component_wise: bool
    ) -> Union[Sequence[np.ndarray], np.ndarray]:
        """Internal function called by WindowedAnomalyScorer ``fit()`` and ``score()`` functions.

        Transforms a (sequence of) series into tabular data of size window `W`. The parameter `component_wise`
        indicates how the rolling window must treat the different components if the series is multivariate.
        If set to False, the rolling window will be done on each component independently. If set to True,
        the `N` components will be concatenated to create windows of size `W` * `N`. The resulting tabular
        data of each series are concatenated if the parameter `concatenate` is set to True.
        """

        list_np_series = [series.all_values(copy=False) for series in list_series]

        if component_wise:

            tabular_data = [
                np.stack(
                    sliding_window_view(arr, window_shape=self.window, axis=0), axis=1
                ).reshape(len(arr[0]), -1, self.window)
                for arr in list_np_series
            ]

        else:

            tabular_data = [
                sliding_window_view(arr, window_shape=self.window, axis=0).reshape(
                    -1, self.window * len(arr[0])
                )
                for arr in list_np_series
            ]

        if concatenate:
            return np.concatenate(tabular_data, axis=1 if component_wise else 0)

        return tabular_data

    def _convert_tabular_to_series(
        self, list_series: Sequence[TimeSeries], list_np_anomaly_score: np.ndarray
    ) -> Sequence[TimeSeries]:
        """Internal function called by WindowedAnomalyScorer  ``score()`` functions when the parameter
        `component_wise` is set to True and the (sequence of) series has more than 1 component.

        Returns the resulting anomaly score as a (sequence of) series, from the np.array `list_np_anomaly_score`
        containing the anomaly score of the (sequence of) series. For efficiency reasons, the anomaly scores were
        computed in one go for each component (component-wise is set to True, so each component has its own fitted
        model). If a list of series is given, each series will be concatenated by its components. The function
        aims to split the anomaly score at the proper indexes to create an anomaly score for each series.
        """
        indice = 0
        result = []
        for series in list_series:
            result.append(
                TimeSeries.from_times_and_values(
                    series.time_index[self.window - 1 :],
                    list(
                        zip(
                            *list_np_anomaly_score[
                                :, indice : indice + len(series) - self.window + 1
                            ]
                        )
                    ),
                )
            )
            indice += len(series) - self.window + 1

        return result

    def _fit_core(
        self,
        list_series: Sequence[TimeSeries],
        *args,
        **kwargs,
    ):
        """Train one sub-model for each component when self.component_wise=True and series is multivariate"""

        if (not self.component_wise) | (list_series[0].width == 1):
            self.model.fit(
                self._tabularize_series(
                    list_series, concatenate=True, component_wise=False
                )
            )
        else:
            tabular_data = self._tabularize_series(
                list_series, concatenate=True, component_wise=True
            )
            # parallelize fitting of the component-wise models
            fit_iterator = zip(tabular_data, [None] * len(tabular_data))
            input_iterator = _build_tqdm_iterator(
                fit_iterator, verbose=False, desc=None, total=tabular_data.shape[1]
            )
            self.model = _parallel_apply(
                input_iterator,
                copy.deepcopy(self.model).fit,
                n_jobs=self._n_jobs,
                fn_args=args,
                fn_kwargs=kwargs,
            )

    @abstractmethod
    def _model_score_method(self, model, data: np.ndarray) -> np.ndarray:
        """Wrapper around model inference method"""
        pass

    def _score_core(self, series: TimeSeries, *args, **kwargs) -> Sequence[TimeSeries]:
        """Apply the scorer (sub) model scoring method on the series components"""
        list_series = [series]
        raise_if_not(
            all([(self.width_trained_on == series.width) for series in list_series]),
            "All series in 'series' must have the same number of components as the data used"
            + f" for training the model, expected {self.width_trained_on} components.",
        )

        if (not self.component_wise) | (list_series[0].width == 1):
            list_np_anomaly_score = [
                self._model_score_method(model=self.model, data=tabular_data)
                for tabular_data in self._tabularize_series(
                    list_series, concatenate=False, component_wise=False
                )
            ]
            list_anomaly_score = [
                TimeSeries.from_times_and_values(
                    series.time_index[self.window - 1 :], np_anomaly_score
                )
                for series, np_anomaly_score in zip(list_series, list_np_anomaly_score)
            ]

        else:
            # parallelize scoring of components by the corresponding sub-model
            score_iterator = zip(
                self.model,
                self._tabularize_series(
                    list_series, concatenate=True, component_wise=True
                ),
            )
            input_iterator = _build_tqdm_iterator(
                score_iterator, verbose=False, desc=None, total=len(self.model)
            )

            list_np_anomaly_score = np.array(
                _parallel_apply(
                    input_iterator,
                    self._model_score_method,
                    n_jobs=self._n_jobs,
                    fn_args=args,
                    fn_kwargs=kwargs,
                )
            )

            list_anomaly_score = self._convert_tabular_to_series(
                list_series, list_np_anomaly_score
            )

        if self.window > 1 and self.window_agg:
            return self._fun_window_agg(list_anomaly_score, self.window)[0]
        else:
            return list_anomaly_score[0]

    def _score_core_from_prediction(
        self, actual_series: TimeSeries, pred_series: TimeSeries
    ) -> TimeSeries:
        diff_series = self._diff_series(actual_series, pred_series)
        return self._score_core(diff_series)


class NLLScorer(AnomalyScorer):
    """Parent class for all LikelihoodScorer"""

    def __init__(
        self,
        window: int = 1,
        diff_fn: str = "abs_diff",
        n_jobs: int = 1,
        window_agg: bool = False,
    ) -> None:
        super().__init__(
            univariate_scorer=False,
            fittable=False,
            single_series_support=False,
            probabilistic_support=True,
            window_agg=window_agg,
            window=window,
            diff_fn=diff_fn,
            n_jobs=n_jobs,
        )

    def _score_core_from_prediction(
        self,
        actual_series: TimeSeries,
        pred_series: TimeSeries,
    ) -> TimeSeries:
        """For each timestamp of the inputs:
            - the parameters of the considered distribution are fitted on the samples of the probabilistic time series
            - the negative log-likelihood of the determinisitc time series values are computed

        If the series is multivariate, the score will be computed on each component independently.

        Parameters
        ----------
        actual_series:
            A determinisict time series (number of samples per timestamp must be equal to 1)
        pred_series
            A probabilistic time series (number of samples per timestamp must be higher than 1)

        Returns
        -------
        TimeSeries
        """
        actual_series = self._extract_deterministic(actual_series, "actual_series")
        self._assert_stochastic(pred_series, "pred_series")

        np_actual_series = actual_series.all_values(copy=False)
        np_pred_series = pred_series.all_values(copy=False)

        np_anomaly_scores = []
        for component_idx in range(pred_series.width):
            np_anomaly_scores.append(
                self._score_core_nllikelihood(
                    # shape actual: (time_steps, )
                    # shape pred: (time_steps, samples)
                    np_actual_series[:, component_idx].squeeze(-1),
                    np_pred_series[:, component_idx],
                )
            )

        anomaly_scores = TimeSeries.from_times_and_values(
            pred_series.time_index, list(zip(*np_anomaly_scores))
        )

        def _window_adjustment_series(series: TimeSeries) -> TimeSeries:
            """Slides a window of size self.window along the input series, and replaces the value of
            the input time series by the mean of the values contained in the window (past self.window
            points, including itself).
            A series of length N will be transformed into a series of length N-self.window+1.
            """

            if self.window == 1:
                # the process results in replacing every value by itself -> return directly the series
                return series
            else:
                return series.window_transform(
                    transforms={
                        "window": self.window,
                        "function": "mean",
                        "mode": "rolling",
                        "min_periods": self.window,
                    },
                    treat_na="dropna",
                )

        return _window_adjustment_series(anomaly_scores)

    @property
    def is_probabilistic(self) -> bool:
        return True

    @abstractmethod
    def _score_core_nllikelihood(self, input_1: Any, input_2: Any) -> Any:
        """For each timestamp, the corresponding distribution is fitted on the probabilistic time-series
        input_2, and returns the negative log-likelihood of the deterministic time-series input_1
        given the distribution.
        """
        pass
