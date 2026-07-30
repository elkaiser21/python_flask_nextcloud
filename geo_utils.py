import math
from geopy.exc import (
  GeocoderTimedOut,
  GeocoderServiceError,
  GeocoderUnavailable,
  GeocoderQuotaExceeded
)
from geopy.geocoders import Nominatim

def bounding_box(lat, lon, radius_km):
  lat_delta = radius_km / 111
  lon_delta = radius_km / (111 * math.cos(math.radians(lat)))

  return (
    lat - lat_delta,
    lat + lat_delta,
    lon - lon_delta,
    lon + lon_delta
  )

def geocode_location(location_str):
  """
  A string indicating a location like a city name is converted into
  latitude, longitude and a message
  """
  message = None
  location = None
  geolocator = Nominatim(user_agent = "geo_search_app", timeout = 10)
  try:
    location = geolocator.geocode(location_str)
  except GeocoderTimedOut:
    message = "Geocoding service timed out"
  except GeocoderQuotaExceeded:
    message = "Geocoding quota exceeded / rate limited"
  except GeocoderUnavailable:
    message = "Geocoding service unavailable"
  except GeocoderServiceError as e:
    message = f"Geocoding service error: {e}"
  if location is None:
    message = f"Location not found: '{location_str}'"

  if message:
    return 0, 0, message
  return location.latitude, location.longitude, message

