from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'get-driving-features/$', views.GetDrivingFeatures.as_view()),
    url(r'get-marginal-driving-features-conjunctive/$', views.GetMarginalDrivingFeaturesConjunctive.as_view()),
    url(r'get-marginal-driving-features/$', views.GetMarginalDrivingFeatures.as_view()),
    
]
