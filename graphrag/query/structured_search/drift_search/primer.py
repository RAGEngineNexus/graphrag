# Copyright (c) 2024 Microsoft Corporation.
# Licensed under the MIT License

"""Primer for DRIFT search."""

import json
import logging
import secrets
import time

import pandas as pd
import tiktoken
from tqdm.asyncio import tqdm_asyncio

from graphrag.config.models.drift_config import DRIFTSearchConfig
from graphrag.model import CommunityReport
from graphrag.query.llm.base import BaseTextEmbedding
from graphrag.query.llm.oai.chat_openai import ChatOpenAI
from graphrag.query.llm.text_utils import num_tokens
from graphrag.query.structured_search.base import SearchResult
from graphrag.query.structured_search.drift_search.system_prompt import (
    DRIFT_PRIMER_PROMPT,
)

log = logging.getLogger(__name__)


class PrimerQueryProcessor:
    """Process the query by expanding it using community reports and generate follow-up actions."""

    def __init__(
        self,
        chat_llm: ChatOpenAI,
        text_embedder: BaseTextEmbedding,
        reports: list[CommunityReport],
        token_encoder: tiktoken.Encoding | None = None,
    ):
        """
        Initialize the PrimerQueryProcessor.

        Args:
            chat_llm (ChatOpenAI): The language model used to process the query.
            text_embedder (BaseTextEmbedding): The text embedding model.
            reports (list[CommunityReport]): List of community reports.
            token_encoder (tiktoken.Encoding, optional): Token encoder for token counting.
        """
        self.chat_llm = chat_llm
        self.text_embedder = text_embedder
        self.token_encoder = token_encoder
        self.reports = reports

    def expand_query(self, query: str) -> tuple[str, int]:
        """
        Expand the query using a random community report template.

        Args:
            query (str): The original search query.

        Returns
        -------
        tuple[str, int]: Expanded query text and the number of tokens used.
        """
        token_ct = 0
        template = secrets.choice(self.reports).full_content # nosec S311

        prompt = f"""Create a hypothetical answer to the following query: {query}\n\n
                  Format it to follow the structure of the template below:\n\n
                  {template}\n"
                  Ensure that the hypothetical answer does not reference new named entities that are not present in the original query."""
     

        messages = [{"role": "user", "content": prompt}]

        text = self.chat_llm.generate(messages)
        token_ct = num_tokens(text + query)
        if text == "":
            log.warning("Failed to generate expansion for query: %s", query)
            return query, token_ct
        return text, token_ct

    def __call__(self, query: str) -> tuple[list[float], int]:
        """
        Call method to process the query, expand it, and embed the result.

        Args:
            query (str): The search query.

        Returns
        -------
        tuple[list[float], int]: List of embeddings for the expanded query and the token count.
        """
        hyde_query, token_ct = self.expand_query(query)
        log.info("Expanded query: %s", hyde_query)
        return self.text_embedder.embed(hyde_query), token_ct


class DRIFTPrimer:
    """Perform initial query decomposition using global guidance from information in community reports."""

    def __init__(
        self,
        config: DRIFTSearchConfig,
        chat_llm: ChatOpenAI,
        token_encoder: tiktoken.Encoding | None = None,
    ):
        """
        Initialize the DRIFTPrimer.

        Args:
            config (DRIFTSearchConfig): Configuration settings for DRIFT search.
            chat_llm (ChatOpenAI): The language model used for searching.
            token_encoder (tiktoken.Encoding, optional): Token encoder for managing tokens.
        """
        self.llm = chat_llm
        self.config = config
        self.token_encoder = token_encoder

    async def decompose_query(
        self, query: str, reports: pd.DataFrame
    ) -> tuple[dict, int]:
        """
        Decompose the query into subqueries based on the fetched global structures.

        Args:
            query (str): The original search query.
            reports (pd.DataFrame): DataFrame containing community reports.

        Returns
        -------
        tuple[dict, int]: Parsed response and the number of tokens used.
        """
        community_reports = "\n\n".join(reports["full_content"].tolist())
        prompt = DRIFT_PRIMER_PROMPT.format(
            query=query, community_reports=community_reports
        )
        messages = [{"role": "user", "content": prompt}]

        response = await self.llm.agenerate(
            messages, response_format={"type": "json_object"}
        )

        parsed_response = json.loads(response)
        token_ct = num_tokens(prompt + response, self.token_encoder)

        return parsed_response, token_ct

    async def asearch(
        self,
        query: str,
        top_k_reports: pd.DataFrame,
    ) -> SearchResult:
        """
        Asynchronous search method that processes the query and returns a SearchResult.

        Args:
            query (str): The search query.
            top_k_reports (pd.DataFrame): DataFrame containing the top-k reports.

        Returns
        -------
        SearchResult: The search result containing the response and context data.
        """
        start_time = time.time()
        report_folds = self.split_reports(top_k_reports)
        tasks = [self.decompose_query(query, fold) for fold in report_folds]
        results_with_tokens = await tqdm_asyncio.gather(*tasks)

        completion_time = time.time() - start_time

        return SearchResult(
            response=[response for response, _ in results_with_tokens],
            context_data={"top_k_reports": top_k_reports},
            context_text=str(top_k_reports),
            completion_time=completion_time,
            llm_calls=2,
            prompt_tokens=sum(tokens for _, tokens in results_with_tokens),
        )

    def split_reports(self, reports: pd.DataFrame) -> list[pd.DataFrame]:
        """
        Split the reports into folds, allowing for parallel processing.

        Args:
            reports (pd.DataFrame): DataFrame of community reports.

        Returns
        -------
        list[pd.DataFrame]: List of report folds.
        """
        folds = []
        num_reports = len(reports)
        primer_folds = self.config.primer_folds or 1  # Ensure at least one fold

        for i in range(primer_folds):
            start_idx = i * num_reports // primer_folds
            end_idx = num_reports if i == primer_folds - 1 else (i + 1) * num_reports // primer_folds
            fold = reports.iloc[start_idx:end_idx]
            folds.append(fold)
        return folds