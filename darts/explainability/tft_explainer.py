from typing import Dict, List, Literal, Optional

import matplotlib.pyplot as plt
import pandas as pd
from torch import Tensor

from darts import TimeSeries
from darts.explainability.explainability import (
    ExplainabilityResult,
    ForecastingModelExplainer,
)
from darts.models import TFTModel

try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal


class TFTExplainer(ForecastingModelExplainer):
    def __init__(
        self,
        model: TFTModel,
    ):
        """
        Explainer class for the TFT model.

        Parameters
        ----------
        model
            The fitted TFT model to be explained.
        """
        super().__init__(model)

        if not model._fit_called:
            raise ValueError("The model needs to be trained before explaining it.")

        self._model = model

    @property
    def encoder_importance(self):
        return self._get_importance(
            weight=self._model.model._encoder_sparse_weights,
            names=self._model.model.encoder_variables,
        )

    @property
    def decoder_importance(self):
        return self._get_importance(
            weight=self._model.model._decoder_sparse_weights,
            names=self._model.model.decoder_variables,
        )

    def get_variable_selection_weight(self, plot=False) -> Dict[str, pd.DataFrame]:
        """Returns the variable selection weight of the TFT model.

        Parameters
        ----------
        plot
            Whether to plot the variable selection weight.

        Returns
        -------
        TimeSeries
            The variable selection weight.

        """

        if plot:
            # plot the encoder and decoder weights
            self._plot_cov_selection(
                self.encoder_importance,
                title="Encoder variable importance",
            )
            self._plot_cov_selection(
                self.decoder_importance,
                title="Decoder variable importance",
            )

        return {
            "encoder_importance": self.encoder_importance,
            "decoder_importance": self.decoder_importance,
        }

    def explain(self, **kwargs) -> ExplainabilityResult:
        """Returns the explainability result of the TFT model.

        The explainability result contains the attention heads of the TFT model.
        The attention heads determine the contribution of time-varying inputs.

        Parameters
        ----------
        kwargs
            Arguments passed to the `predict` method of the TFT model.

        Returns
        -------
        ExplainabilityResult
            The explainability result containing the attention heads.

        """
        super().explain()
        # without the predict call, the weights will still bet set to the last iteration of the forward() method
        # of the _TFTModule class
        if "n" not in kwargs:
            kwargs["n"] = self._model.model.output_chunk_length

        _ = self._model.predict(**kwargs)

        # get the weights and the attention head from the trained model for the prediction
        attention_heads = (
            self._model.model._attn_out_weights.squeeze().sum(axis=1).detach()
        )

        # return the explainer result to be used in other methods
        return ExplainabilityResult(
            {
                0: {
                    "attention_heads": TimeSeries.from_values(attention_heads.T),
                }
            },
        )

    @staticmethod
    def plot_attention_heads(
        expl_result: ExplainabilityResult,
        plot_type: Optional[Literal["all", "time", "heatmap"]] = "time",
    ):
        """Plots the attention heads of the TFT model."""
        attention_heads = expl_result.get_explanation(
            component="attention_heads",
            horizon=0,
        )
        if plot_type == "all":
            fig = plt.figure()
            attention_heads.plot(
                label="Attention Head",
                max_nr_components=-1,
                figure=fig,
            )
            # move legend to the right side of the figure
            plt.legend(bbox_to_anchor=(0.95, 1), loc="upper left")
            plt.xlabel("Time steps in the past (# lags)")
            plt.ylabel("Attention")
        elif plot_type == "time":
            fig = plt.figure()
            attention_heads.mean(1).plot(label="Mean Attention Head", figure=fig)
            plt.xlabel("Time steps in the past (# lags)")
            plt.ylabel("Attention")
        elif plot_type == "heatmap":
            avg_attention = attention_heads.values().transpose()
            fig = plt.figure()
            plt.imshow(avg_attention, cmap="hot", interpolation="nearest", figure=fig)
            plt.xlabel("Time steps in the past (# lags)")
            plt.ylabel("Horizon")
        else:
            raise ValueError("`plot_type` must be either 'all', 'time' or 'heatmap'")

    def _get_importance(
        self,
        weight: Tensor,
        names: List[str],
        n_decimals=3,
    ) -> pd.DataFrame:
        """Returns the encoder or decoder variable of the TFT model.

        Parameters
        ----------
        weights
            The weights of the encoder or decoder of the trained TFT model.
        names
            The encoder or decoder names saved in the TFT model class.
        n_decimals
            The number of decimals to round the importance to.

        Returns
        -------
        pd.DataFrame
            The importance of the variables.
        """
        # transform the encoder/decoder weights to percentages, rounded to n_decimals
        weights_percentage = (
            weight.mean(axis=1).detach().numpy().mean(axis=0).round(n_decimals) * 100
        )

        # create a dataframe with the variable names and the weights
        name_mapping = self._name_mapping
        importance = pd.DataFrame(
            weights_percentage,
            columns=[name_mapping[name] for name in names],
        )

        # return the importance sorted descending
        return importance.transpose().sort_values(0, ascending=False).transpose()

    @property
    def _name_mapping(self) -> Dict[str, str]:
        """Returns the feature name mapping of the TFT model.

        Returns
        -------
        Dict[str, str]
            The feature name mapping. For example
            {
                'past_covariate_0': 'heater',
                'past_covariate_1': 'year',
                'past_covariate_2': 'month',
                'future_covariate_0': 'darts_enc_fc_cyc_month_sin',
                'future_covariate_1': 'darts_enc_fc_cyc_month_cos',
                'target_0': 'ice cream',
             }

        """
        past_covariates_name_mapping = {
            f"past_covariate_{i}": colname
            for i, colname in enumerate(self._model.past_covariate_series.components)
        }
        future_covariates_name_mapping = {
            f"future_covariate_{i}": colname
            for i, colname in enumerate(self._model.future_covariate_series.components)
        }
        target_name_mapping = {
            f"target_{i}": colname
            for i, colname in enumerate(self._model.training_series.components)
        }

        return {
            **past_covariates_name_mapping,
            **future_covariates_name_mapping,
            **target_name_mapping,
        }

    @staticmethod
    def _plot_cov_selection(
        importance: pd.DataFrame, title: str = "Variable importance"
    ):
        """Plots the variable importance of the TFT model.

        Parameters
        ----------
        importance
            The encoder / decoder importance.
        title
            The title of the plot.

        """
        fig = plt.figure()
        plt.bar(importance.columns.tolist(), importance.values[0].tolist(), figure=fig)
        plt.title(title)
        plt.xlabel("Variable", fontsize=12)
        plt.ylabel("Variable importance in %")
        plt.show()
