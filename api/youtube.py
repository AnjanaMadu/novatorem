"""YouTube Data API integration for the latest music-like liked video."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .config import youtube_config
from .cache import get_json, set_json
from .exceptions import APIError, AuthenticationError, NoTracksError


@dataclass
class TrackInfo:
    """Normalized track information."""

    track_name: str
    artist_name: str
    album_art_url: str
    track_url: str
    artist_url: str


class YouTubeTokenManager:
    """Thread-safe manager for Google OAuth access tokens."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._lock = threading.Lock()

    def _refresh_token(self) -> str:
        try:
            response = requests.post(
                youtube_config.token_url,
                data={
                    "client_id": youtube_config.client_id,
                    "client_secret": youtube_config.client_secret,
                    "refresh_token": youtube_config.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
                verify=False,
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if not token:
                raise AuthenticationError("YouTube", "No access token in response")
            return token
        except requests.RequestException as error:
            raise AuthenticationError("YouTube", str(error)) from error

    def get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if self._token is None or force_refresh:
                self._token = self._refresh_token()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


_token_manager = YouTubeTokenManager()


def is_configured() -> bool:
    """Return whether YouTube OAuth credentials are configured."""
    return youtube_config.is_configured()


def _api_get(endpoint: str, params: dict[str, Any], retry_on_auth_error: bool = True) -> dict[str, Any]:
    """Call a YouTube Data API endpoint with OAuth authentication."""
    try:
        response = requests.get(
            f"{youtube_config.api_url}/{endpoint}",
            params=params,
            headers={"Authorization": f"Bearer {_token_manager.get_token()}"},
            timeout=10,
            verify=False,
        )
        if response.status_code == 401 and retry_on_auth_error:
            _token_manager.invalidate()
            return _api_get(endpoint, params, retry_on_auth_error=False)
        if not response.ok:
            raise APIError("YouTube", response.status_code, response.text)
        return response.json()
    except requests.RequestException as error:
        raise APIError("YouTube", 0, str(error)) from error


def _get_likes_playlist_id() -> str:
    data = _api_get("channels", {"part": "contentDetails", "mine": "true"})
    items = data.get("items", [])
    if not items:
        raise NoTracksError("YouTube")
    playlist_id = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("likes", "")
    if not playlist_id:
        raise NoTracksError("YouTube")
    return playlist_id


def _find_music_video(playlist_id: str) -> Optional[TrackInfo]:
    page_token = ""

    for _ in range(5):
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        playlist_data = _api_get("playlistItems", params)
        video_ids = [
            item.get("contentDetails", {}).get("videoId", "")
            for item in playlist_data.get("items", [])
        ]
        video_ids = [video_id for video_id in video_ids if video_id]

        if video_ids:
            videos_data = _api_get(
                "videos",
                {"part": "snippet,status", "id": ",".join(video_ids)},
            )
            videos_by_id = {item.get("id"): item for item in videos_data.get("items", [])}

            for video_id in video_ids:
                video = videos_by_id.get(video_id, {})
                snippet = video.get("snippet", {})
                if snippet.get("categoryId") != "10":
                    continue

                channel_id = snippet.get("channelId", "")
                return TrackInfo(
                    track_name=snippet.get("title", "Unknown Track"),
                    artist_name=snippet.get("channelTitle", "Unknown Artist").removesuffix(
                        " - Topic"
                    ),
                    album_art_url=snippet.get("thumbnails", {}).get("high", {}).get(
                        "url",
                        snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                    ),
                    track_url=f"https://music.youtube.com/watch?v={video_id}",
                    artist_url=(
                        f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
                    ),
                )

        page_token = playlist_data.get("nextPageToken", "")
        if not page_token:
            break

    return None


def get_now_playing() -> dict[str, Any]:
    """Return the newest liked video in YouTube's Music category."""
    cached_track = get_json("youtube:music-liked")
    if cached_track is not None:
        return cached_track

    track = _find_music_video(_get_likes_playlist_id())
    if track is None:
        raise NoTracksError("YouTube music likes")

    track_data = {
        "is_playing": False,
        "status": "Recently liked:",
        "track_name": track.track_name,
        "artist_name": track.artist_name,
        "album_name": "YouTube Music",
        "album_art_url": track.album_art_url,
        "track_url": track.track_url,
        "artist_url": track.artist_url,
        "audio_features": None,
    }
    set_json("youtube:music-liked", track_data)
    return track_data