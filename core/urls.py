from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.index, name='index'),
    path('docs/', views.docs, name='docs'),
    path('preview/', views.preview, name='preview'),
    path('upload/', views.upload, name='upload'),
    path('params/', views.params_schema, name='params'),
    path('jobs/<str:job_id>/', views.job, name='job'),
    path('jobs/<str:job_id>/cancel/', views.job_cancel, name='job_cancel'),
    path('jobs/<str:job_id>/revise/', views.job_revise, name='job_revise'),
    path('jobs/<str:job_id>/move/', views.job_move, name='job_move'),
    path('jobs/<str:job_id>/3d/', views.job_3d, name='job_3d'),
    path('jobs/<str:job_id>/scene/', views.job_scene, name='job_scene'),
    path('jobs/<str:job_id>/<str:kind>/', views.job_result, name='job_result'),
]
