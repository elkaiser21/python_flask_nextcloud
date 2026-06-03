from geopy.geocoders import Nominatim

def geocode_location(location_str):
    geolocator = Nominatim(user_agent="geo_search_app")
    location = geolocator.geocode(location_str)
    print("LOCATION = " + location_str)
    print(location)
    if not location:
        raise ValueError("Location not found")
    return location.latitude, location.longitude



lat, lon = geocode_location("arnhem")

print(lat, lon)
