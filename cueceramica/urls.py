from django.urls import path
from .views import CueCeramicaProductsAPIView

urlpatterns = [
    path("cueceramica-products/", CueCeramicaProductsAPIView.as_view(), name="cueceramica-products"),
]