import geo_utils
import utils
from dataclasses import dataclass, field

@dataclass
class HeatmapFilters:
    min_visits: int = 1
    max_visits: int | None = None
    min_km: float = 5
    max_km: float = 40
    max_results: int = 3000
    min_lon: float | None = None
    max_lon: float | None = None
    min_date: str = "01-01-2025"
    max_date: str = ""
    min_lat: float | None = None
    max_lat: float | None = None
    min_lon: float | None = None
    max_lon: float | None = None
    location: str = "Apeldoorn"
    km_radius: int = 5
    track_types: list[str] = field(default_factory=lambda: ["Cycle", "Walk"])

    def clear_values(self):
      self.min_visits = 1
      self.max_results = 3000

      self.max_visits = None
      self.min_km = None
      self.max_km = None

      self.min_date = ""
      self.max_date = ""

      self.location = ""
      self.km_radius = None

      self.min_lat = None
      self.max_lat = None
      self.min_lon = None
      self.max_lon = None

      self.track_types = []

    def read_from_form(self, form):
      self.min_visits = utils.to_int(form.get("min_visits")) or 1
      self.max_visits = utils.to_int(form.get("max_visits"))

      self.min_km = utils.to_float(form.get("min_km"))
      self.max_km = utils.to_float(form.get("max_km"))

      self.max_results = utils.to_int(form.get("max_results")) or 5000

      self.min_date = form["min_date"]
      self.max_date = form["max_date"]

      self.min_lat = utils.to_float(form.get("min_lat"))
      self.max_lat = utils.to_float(form.get("max_lat"))

      self.min_lon = utils.to_float(form.get("min_lon"))
      self.max_lon = utils.to_float(form.get("max_lon"))

      self.location = form.get("location")
      self.km_radius = utils.to_int(form.get("km_radius"))

      get_bounding_box_for_location(self)

      self.track_types = form.getlist("track_type")

def get_bounding_box_for_location(filters: HeatmapFilters):
  if filters.location:
    if filters.km_radius is None:
      filters.km_radius = 10

    center_lat, center_lon, message = geo_utils.geocode_location(filters.location)
    filters.min_lat, filters.max_lat, filters.min_lon, filters.max_lon = geo_utils.bounding_box(center_lat, center_lon,
                                                                                                filters.km_radius)