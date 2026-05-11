from django.urls import path

from .news_system import get_news_article
from .views import mark_notification_read, purchase_item, get_server_date, collect_reward, admob_verify_reward, \
    get_world_ranking, upgrade_runner, purchase_package, player_sync, economy_sync, initialize_player_data, \
    get_data_row, get_data_manifest
from .validation import validation_data_listener, fetch_data, invalidate_runners
from .notification_system import delete_notification

urlpatterns = [
    path('purchase_item/', purchase_item, name='purchase_item'),
    path('get_server_date/', get_server_date, name='get_server_date'),
    path('collect_reward/', collect_reward, name="collect_reward"),
    path('admob_verify_reward/', admob_verify_reward, name='admob_verify_reward'),
    path('get_world_ranking/', get_world_ranking, name='get_world_ranking'),
    path('upgrade_runner/', upgrade_runner, name='upgrade_runner'),
    path('purchase_package/', purchase_package, name='purchase_package'),
    path('player_sync/', player_sync, name='player_sync'),
    path('economy_sync/', economy_sync, name='economy_sync'),
    path('initialize_player_data/', initialize_player_data, name='initialize_player_data'),
    path('mark_notification_read/', mark_notification_read, name='mark_notification_read'),
    path('delete_notification/', delete_notification, name='delete_notification'),
    path('data_updates_listener/', validation_data_listener, name='data_updates_listener'),
    path("fetch_data/", fetch_data, name="fetch_data"),
    # path("get_news_article/<uuid:article_uid>/", get_news_article, name="get_news_article"),
    path("get_data_manifest/", get_data_manifest, name="get_data_manifest"),
    path("get_data_row/", get_data_row, name="get_data_row"),
    path("invalidate_runners/", invalidate_runners, name="invalidate_runners"),
]
