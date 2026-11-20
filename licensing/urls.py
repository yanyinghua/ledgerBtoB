from django.urls import path

from .views import license_page

app_name = 'licensing'

urlpatterns = [
    path('', license_page, name='license'),
]
