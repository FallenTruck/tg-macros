import asyncio
from typing import List, Optional

import httpx

from .models import CatalogOverlapDecision, CatalogSuggestion, FoodCatalogEntry, MealEstimate, RecommendationRequest, RecommendationResult


class MacroEstimatorError(RuntimeError):
    """Raised when macro estimation API call fails."""


class RecommendationError(RuntimeError):
    """Raised when recommendation API call fails."""


class CatalogReviewError(RuntimeError):
    """Raised when catalog overlap review API call fails."""


class MacroEstimatorClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    async def estimate(self, image_bytes: bytes, caption: str, persona_hint: str = "") -> MealEstimate:
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                data = {"caption": caption}
                if persona_hint:
                    data["persona_hint"] = persona_hint

                if self._client:
                    response = await self._client.post(
                        self._base_url,
                        files={"file": ("meal.jpg", image_bytes, "image/jpeg")},
                        data=data,
                        timeout=self._timeout,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            self._base_url,
                            files={"file": ("meal.jpg", image_bytes, "image/jpeg")},
                            data=data,
                            timeout=self._timeout,
                        )
                response.raise_for_status()
                payload = response.json()
                return MealEstimate.from_api_payload(payload)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as err:
                last_error = err
                if attempt < self._max_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                break
            except httpx.HTTPStatusError as err:
                detail = err.response.text if err.response is not None else str(err)
                raise MacroEstimatorError(f"API returned error status: {detail[:120]}") from err
            except (ValueError, TypeError) as err:
                raise MacroEstimatorError(f"Invalid API response ({str(err)[:120]})") from err

        raise MacroEstimatorError(f"Network error: {str(last_error)[:120]}")


class RecommendationClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 45.0,
        max_retries: int = 1,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    async def recommend(self, request: RecommendationRequest) -> RecommendationResult:
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                payload = request.to_payload()
                if self._client:
                    response = await self._client.post(
                        self._base_url,
                        json=payload,
                        timeout=self._timeout,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            self._base_url,
                            json=payload,
                            timeout=self._timeout,
                        )
                response.raise_for_status()
                return RecommendationResult.from_payload(response.json())
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as err:
                last_error = err
                if attempt < self._max_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                break
            except httpx.HTTPStatusError as err:
                detail = err.response.text if err.response is not None else str(err)
                raise RecommendationError(f"API returned error status: {detail[:120]}") from err
            except (ValueError, TypeError) as err:
                raise RecommendationError(f"Invalid recommendation response ({str(err)[:120]})") from err

        raise RecommendationError(f"Network error: {str(last_error)[:120]}")


class CatalogReviewClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    async def review_overlaps(
        self,
        suggestions: List[CatalogSuggestion],
        catalog_entries: List[FoodCatalogEntry],
    ) -> List[CatalogOverlapDecision]:
        last_error: Optional[Exception] = None
        payload = {
            "suggestions": [item.to_payload() for item in suggestions],
            "catalog_entries": [item.to_payload() for item in catalog_entries],
        }

        for attempt in range(self._max_retries + 1):
            try:
                if self._client:
                    response = await self._client.post(
                        self._base_url,
                        json=payload,
                        timeout=self._timeout,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            self._base_url,
                            json=payload,
                            timeout=self._timeout,
                        )
                response.raise_for_status()
                body = response.json()
                decisions = body.get("decisions")
                if not isinstance(decisions, list):
                    raise ValueError("decisions must be a list")
                return [CatalogOverlapDecision.from_payload(item) for item in decisions]
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as err:
                last_error = err
                if attempt < self._max_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                break
            except httpx.HTTPStatusError as err:
                detail = err.response.text if err.response is not None else str(err)
                raise CatalogReviewError(f"API returned error status: {detail[:120]}") from err
            except (ValueError, TypeError) as err:
                raise CatalogReviewError(f"Invalid catalog review response ({str(err)[:120]})") from err

        raise CatalogReviewError(f"Network error: {str(last_error)[:120]}")
