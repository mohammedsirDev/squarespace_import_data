from rest_framework import serializers


class ProductBodySerializer(serializers.Serializer):
    expected_images = serializers.IntegerField(min_value=0)
    translations = serializers.CharField()
    is_visible = serializers.BooleanField(default=True)
    inspiration_country = serializers.ListField(child=serializers.CharField())
    category = serializers.IntegerField()
    shop = serializers.IntegerField()
    shipping_profiles = serializers.ListField(child=serializers.IntegerField())
    product_attributes = serializers.ListField(child=serializers.DictField(), default=list)
    inventory = serializers.ListField(child=serializers.DictField())
    customizable = serializers.BooleanField(default=False)