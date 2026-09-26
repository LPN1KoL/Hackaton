from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.index, name='index'),
    path('docs/', views.docs, name='docs'),
    path('preview/', views.preview, name='preview'),
    path('upload/', views.upload, name='upload'),
    path('jobs/<str:job_id>/', views.job, name='job'),
    path('jobs/<str:job_id>/<str:kind>/', views.job_result, name='job_result'),
]
