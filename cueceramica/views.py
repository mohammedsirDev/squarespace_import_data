
# Create your views here.
from typing import Any
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import ProductBodySerializer
from .services import get_instant_catalog


class CueCeramicaProductsAPIView(APIView):
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        raw_lang = (
            request.query_params.get("lang")
            or request.query_params.get("wglang")
            or "fr"
        ).lower()
        target_lang = "en" if "en" in raw_lang else ("es" if "es" in raw_lang else "fr")
        force_refresh = bool(request.query_params.get("refresh"))

        results = get_instant_catalog(target_lang, force_refresh=force_refresh)

        serializer = ProductBodySerializer(data=results, many=True)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)