import logging
import math
import os

import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import unquote

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import mysql.connector
import requests

import folium
import h3
from branca.colormap import linear

from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, abort, Response

import utils
import geo_utils
import classes

logging.basicConfig(level = logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto = 1, x_host = 1)

GPX_DIR = "/home/w7667388/trackfiles"

NEXTCLOUD_USER = os.environ.get("NEXTCLOUD_USER")
NEXTCLOUD_PASSWORD = os.environ.get("NEXTCLOUD_PASSWORD")
NEXTCLOUD_DAV_URL = os.environ.get("NEXTCLOUD_DAV_URL")
NEXTCLOUD_PHOTO_DIR = os.environ.get("NEXTCLOUD_PHOTO_SYNC_DIR")
NEXTCLOUD_PHOTO_ARCHIVE_DIR = os.environ.get("NEXTCLOUD_PHOTO_ARCHIVE_DIR")
NEXTCLOUD_TRACK_DIR = os.environ.get("NEXTCLOUD_TRACK_SYNC_DIR")

@app.context_processor
def inject_path():
  return {"current_path": request.path}

app.config.update(
  SESSION_COOKIE_SECURE = True,
  SESSION_COOKIE_HTTPONLY = True,
  SESSION_COOKIE_SAMESITE = "Lax",
)

db_config = {
  "host": "localhost",
  "user": os.environ.get("DB_USER"),
  "password": os.environ.get("DB_PASSWORD"),
  "database": os.environ.get("DB_NAME")
}

def get_user(username):
  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)

  cursor.execute(
    "SELECT * FROM users WHERE username = %s",
    (username,)
  )
  user = cursor.fetchone()

  cursor.close()
  conn.close()
  return user

def login_required(view):
  from functools import wraps

  @wraps(view)
  def wrapped(*args, **kwargs):
    if "user_id" not in session:
      return redirect(url_for("login"))
    return view(*args, **kwargs)

  return wrapped


def get_tracks_by_latlon(location, radius):
  """
  A location string and a radius in km is selecting all the tracks within that area.
  For the selection of tracks the middle lat-lon is used
  """
  center_lat, center_lon, message = geo_utils.geocode_location(location)
  if message:
    return [], message
  min_lat, max_lat, min_lon, max_lon = geo_utils.bounding_box(center_lat, center_lon, radius)

  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)
  query = """
    SELECT
            t.name,
            t.date_taken,
            t.extra_info,
            t.type,
            ROUND(t.length/1000, 2) as len,
            ROUND(t.middle_lat, 6) as lat,
            ROUND(t.middle_lon, 6) as lon,
        (
            6371 * ACOS(
                COS(RADIANS(%s)) *
                COS(RADIANS(t.middle_lat)) *
                COS(RADIANS(t.middle_lon) - RADIANS(%s)) +
                SIN(RADIANS(%s)) *
                SIN(RADIANS(t.middle_lat))
            )
        ) AS distance
    FROM track t
    WHERE
        t.middle_lat BETWEEN %s AND %s
        AND t.middle_lon BETWEEN %s AND %s
    HAVING distance <= %s
    ORDER BY distance;
    """

  params = (
    center_lat, center_lon, center_lat,
    min_lat, max_lat,
    min_lon, max_lon,
    radius
  )

  cursor.execute(query, params)
  results = cursor.fetchall()

  cursor.close()
  conn.close()

  return results, message


def get_tracks_by_keyword(keywords):
  """
  Select all tracks with the given (part of) keyword.
  """
  placeholders = ",".join(["%s"] * len(keywords))
  params = keywords + [len(keywords)]

  sql = f"""
        SELECT DISTINCT
            name,
            date_taken,
            extra_info,
            type,
            ROUND(length/1000, 2) as len,
            ROUND(middle_lat, 6) as lat,
            ROUND(middle_lon, 6) as lon
        FROM v_tracks_by_keyword
        WHERE LOWER(keyword) IN ({placeholders})
        GROUP BY name
        HAVING COUNT(DISTINCT keyword) = %s
        ORDER BY date_taken DESC
    """

  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)
  cursor.execute(sql, params)
  results = cursor.fetchall()

  cursor.close()
  conn.close()

  return results


def get_tracks_by_date(selected_date):
  """
  Select all tracks with the given date.
  """
  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)

  sql = """
        SELECT
            name,
            date_taken,
            extra_info,
            type,
            ROUND(length/1000, 2) as len,
            ROUND(middle_lat, 6) as lat,
            ROUND(middle_lon, 6) as lon
        FROM track
        WHERE DATE(date_taken) = %s
      """

  cursor.execute(sql, (selected_date,))
  results = cursor.fetchall()
  cursor.close()
  conn.close()

  return results


def get_photos_by_keywords(keywords):
  """
  Select all photos with the given (part of) keyword.
  """
  placeholders = ",".join(["%s"] * len(keywords))
  params = keywords + [len(keywords)]
  # logger.info("placeholders: %r", placeholders)
  # logger.info("params: %r", params)

  sql = f"""
        SELECT DISTINCT
            name,
            date,
            extra_info,
            title,
            ROUND(latitude, 6) as lat,
            ROUND(longitude, 6) as lon
        FROM v_photo_by_keyword
        WHERE LOWER(keyword) IN ({placeholders})
        GROUP BY name
        HAVING COUNT(DISTINCT keyword) = %s
        ORDER BY date DESC
    """

  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)
  cursor.execute(sql, params)
  results = cursor.fetchall()

  cursor.close()
  conn.close()

  return results


def get_photo_info(filename, date_taken):
  """
  Get photo information
  """
  sql = """
        SELECT
          f.id,
          f.date_taken,
          f.title,
          f.extra_info,
          f.filename,
          GROUP_CONCAT(k.keyword ORDER BY k.keyword SEPARATOR ', ') AS keywords
        FROM foto f
        LEFT JOIN foto_keywords k
          ON k.foto_id = f.id
        WHERE f.filename = %s AND f.date_taken = %s 
        GROUP BY
          f.id,
          f.date_taken,
          f.title,
          f.extra_info,
          f.filename;
        """

  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)
  cursor.execute(sql, (f"{filename}", f"{date_taken}"))
  results = cursor.fetchall()

  cursor.close()
  conn.close()

  return results


def get_keywords_by_searchstring(searchstring, mode):
  """
  Select all keywords with the given (part of) keyword.
  Depending on the choosen mode it with be the keywords of tracks or photos
  """
  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)
  sql = ""

  if mode == "photo":
    sql = """
         SELECT
            keyword,
            COUNT(*) as amount
          FROM foto_keywords
          WHERE LOWER(keyword) LIKE %s
          GROUP BY keyword
          ORDER BY amount DESC
      """
  elif mode == "track":
    sql = """
         SELECT
            keyword,
            COUNT(*) as amount
          FROM track_keywords
          WHERE LOWER(keyword) LIKE %s
          GROUP BY keyword
          ORDER BY amount DESC
      """

  cursor.execute(sql, (f"%{searchstring}%",))
  results = cursor.fetchall()

  cursor.close()
  conn.close()

  return results


def get_dates_by_searchstring(searchstring, mode):
  """
  Select all dates with the given (part of) datestring.
  Depending on the choosen mode it with be the dates of tracks or photos
  """
  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)
  sql = ""

  if mode == "photo":
    sql = """
         SELECT
            date_taken,
            COUNT(*) as amount
          FROM foto
          WHERE date_taken LIKE %s
          GROUP BY date_taken
          ORDER BY date_taken DESC
      """
  elif mode == "track":
    sql = """
         SELECT
            date_taken,
            COUNT(*) as amount
          FROM track
          WHERE date_taken LIKE %s
          GROUP BY date_taken
          ORDER BY date_taken DESC
      """

  cursor.execute(sql, (f"%{searchstring}%",))
  results = cursor.fetchall()

  cursor.close()
  conn.close()

  return results


def get_photos_by_date(selected_date):
  """
  Select all photos with the given date.
  """
  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)

  sql = """
        SELECT
            filename as name,
            date_taken,
            extra_info,
            title,
            ROUND(lat, 6) as lat,
            ROUND(lon, 6) as lon
        FROM foto
        WHERE DATE(date_taken) = %s
    """

  cursor.execute(sql, (selected_date,))
  results = cursor.fetchall()
  cursor.close()
  conn.close()

  return results


def check_if_track_exists(selected_date):
  """
  See if there is a track for the given date
  """
  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)

  sql = """
        SELECT count(*) as count
        FROM track
        WHERE DATE(date_taken) = %s
    """
  cursor.execute(sql, (selected_date,))
  results = cursor.fetchall()
  cursor.close()
  conn.close()

  return results


@app.route("/login", methods = ["GET", "POST"])
def login():
  error = None

  if request.method == "POST":
    username = request.form["username"]
    password = request.form["password"]

    user = get_user(username)

    if user and check_password_hash(user["password_hash"], password):
      session["user_id"] = user["id"]
      session["username"] = user["username"]
      return redirect(url_for("index"))

    error = "Invalid username or password"

  return render_template("login.html", error = error)


@app.route("/logout")
def logout():
  session.clear()
  return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
  return render_template("home.html")


@app.route("/track_by_keyword", methods = ["GET", "POST"])
@login_required
def track_by_keyword():
  tracks = []
  keyword_str = request.args.get("keyword", "").strip()

  if keyword_str:
    """keywords can be split by comma"""
    keywords = [keyword.strip() for keyword in keyword_str.split(",")]
    tracks = get_tracks_by_keyword(keywords)

  return render_template("track_by_keyword.html", tracks = tracks, keyword = keyword_str)


@app.route("/photo_by_keyword", methods = ["GET", "POST"])
@login_required
def photo_by_keyword():
  photos = []
  keyword_str = request.args.get("keyword", "").strip()

  if keyword_str:
    """keywords can be split by comma"""
    keywords = [keyword.strip() for keyword in keyword_str.split(",")]
    photos = get_photos_by_keywords(keywords)

  return render_template("photo_by_keyword.html", photos = photos, keyword = keyword_str)


@app.route("/photo_by_date", methods = ["GET"])
@login_required
def photo_by_date():
  date_str = request.args.get("date")  # e.g. "2025-01-15"
  showphotos = request.args.get("showphotos")
  photos = []
  tracks_exists = False

  if date_str:
    if showphotos:
      year, month, day = map(int, date_str.split("-"))
      return redirect(
        url_for(
          "photos_by_date",
          year = year,
          month = month,
          day = day,
        )
      )
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    photos = get_photos_by_date(selected_date)
    row = check_if_track_exists(selected_date)[0]
    tracks_exists = bool(row["count"]) if row else False

  return render_template("photo_by_date.html",
                         photos = photos,
                         selected_date = date_str,
                         showphotos = showphotos,
                         tracks_exists = tracks_exists)


@app.route("/track_by_location", methods = ["GET", "POST"])
@login_required
def track_by_location():
  tracks = []
  location = ""
  message = ""
  kmradius = 5

  if request.method == "POST":
    location = request.form.get("location", "").strip()
    kmradius = request.form.get("kmradius", 5).strip()
    if location:
      tracks, message = get_tracks_by_latlon(location, int(kmradius))

  return render_template("track_by_location.html",
                         tracks = tracks,
                         location = location,
                         kmradius = kmradius,
                         message = message)


@app.route("/download/<path:filename>")
def download_gpx(filename):
  filename = unquote(filename)

  if not filename.endswith(".gpx"):
    abort(400)

  file_path = os.path.join(GPX_DIR, filename)
  if not os.path.isfile(file_path):
    abort(404)

  return send_from_directory(
    GPX_DIR,
    filename,
    as_attachment = True
  )


@app.route("/download_track/<int:year>/<int:month>/<int:day>/<path:filename>")
def download_track(year, month, day, filename):
  filename = unquote(filename)
  if not filename.endswith(".gpx"):
    abort(400)

  url = (
    f"{NEXTCLOUD_DAV_URL}"
    f"{NEXTCLOUD_TRACK_DIR}/"
    f"{year}/{month}/{day}/{filename}"
  )

  r = requests.get(
    url,
    auth = (NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
    stream = True,
    verify = False  # self-signed cert (LAN)
  )

  if r.status_code != 200:
    abort(404)

  response = Response(
    r.iter_content(chunk_size = 8192)
  )

  # 🔽 Force download in browser
  response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
  response.headers["Content-Type"] = r.headers.get(
    "Content-Type", "application/octet-stream"
  )

  return response


@app.route("/keywordsearch")
@login_required
def keywordsearch():
  keywords = []

  searchstring = request.args.get("searchstring", "").strip()
  mode = request.args.get("mode")
  if searchstring:
    keywords = get_keywords_by_searchstring(searchstring, mode)

  return render_template("keywords.html", keywords = keywords, searchstring = searchstring, mode = mode)


@app.route("/datesearch")
@login_required
def datesearch():
  dates = []

  searchstring = request.args.get("searchstring", "").strip()
  mode = request.args.get("mode")
  if searchstring:
    dates = get_dates_by_searchstring(searchstring, mode)

  return render_template("dates.html", dates = dates, searchstring = searchstring, mode = mode)


@app.route("/track_by_date")
@login_required
def track_by_date():
  date_str = request.args.get("date")  # e.g. "2025-01-15"
  tracks = []

  if date_str:
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    tracks = get_tracks_by_date(selected_date)

  return render_template("track_by_date.html", tracks = tracks, selected_date = date_str)


@app.route("/photos/<int:year>/<int:month>/<int:day>")
@login_required
def photos_by_date(year, month, day):
  url = get_nextcloud_url(NEXTCLOUD_PHOTO_DIR, year, month, day, None)
  r = get_photos_request(url)

  if r.status_code not in (207, 200):
    url = get_nextcloud_url(NEXTCLOUD_PHOTO_ARCHIVE_DIR, year, month, day, None)
    r = get_photos_request(url)
    if r.status_code not in (207, 200):
      return f"Nextcloud error: {r.status_code}", 500

  tree = ET.fromstring(r.text)
  ns = {"d": "DAV:"}

  photos = []
  for resp in tree.findall("d:response", ns):
    href = resp.find("d:href", ns).text
    if href.lower().endswith(".jpg"):
      filename = unquote(href.split("/")[-1])
      photos.append(filename)
      # logger.info("href=%r filename=%r", href, filename)

  return render_template(
    "photos.html",
    year = year,
    month = month,
    day = day,
    photos = photos,
  )


@app.route("/show_photo/<int:year>/<int:month>/<int:day>/<path:filename>")
@login_required
def show_photo(year, month, day, filename):
  url = get_nextcloud_url(NEXTCLOUD_PHOTO_DIR, year, month, day, filename)
  r = get_show_photo_request(url)

  if r.status_code != 200:
    url = get_nextcloud_url(NEXTCLOUD_PHOTO_ARCHIVE_DIR, year, month, day, filename)
    r = get_show_photo_request(url)
    if r.status_code != 200:
      abort(r.status_code)

  mimetype = r.headers.get("Content-Type")
  return Response(
    r.iter_content(chunk_size = 8192),
    mimetype = mimetype or "image/jpeg",
  )


@app.route("/photo_info/<int:year>/<int:month>/<int:day>/<path:filename>")
@login_required
def photo_info(year, month, day, filename):
  photo = {}
  safe_filename = unquote(filename)

  url = get_nextcloud_url(NEXTCLOUD_PHOTO_DIR, year, month, day, filename)
  r = get_photo_info_request(url)

  if r.status_code != 200:
    url = get_nextcloud_url(NEXTCLOUD_PHOTO_ARCHIVE_DIR, year, month, day, filename)
    r = get_photo_info_request(url)
    if r.status_code != 200:
      abort(r.status_code)

  if get_photo_info(safe_filename, f"{year}-{month}-{day}"):
    row = get_photo_info(safe_filename, f"{year}-{month}-{day}")[0]
    """remove duplicate keywords"""
    keywords = []
    if row["keywords"]:
      keywords = sorted(set(
        kw.strip() for kw in row["keywords"].split(",")
        if kw.strip()
      ))

    photo = {
      **row,
      "keywords": keywords
    }

  return render_template(
    "photo_info.html",
    year = year,
    month = month,
    day = day,
    filename = safe_filename,
    photo = photo,
  )


def get_tracks(cursor, cell_id, where_clause, params):
  result = ""
  sql = f"""
    SELECT
      t.date_taken,
      t.name, 
      t.type
    FROM track_h3 h
    JOIN track t
    ON h.track_name=t.name
    {where_clause} 
    AND h.cell_id = '{cell_id}'
    """
  # logger.info(f"sql get tracks: {sql}")
  # logger.info(f"params get tracks: {params}")

  cursor.execute(sql, params)
  rows = cursor.fetchall()
  for row in rows:
    result += f"<li><a target=\"_blank\" rel=\"noopener noreferrer\" href=\"/track_by_date?date={row['date_taken']}\">{row['date_taken']} - {row['name']} - {row['type']}</a></li>"
  return result


@app.route("/heatmap", methods=["GET", "POST"])
@login_required
def heatmap():
  filters = classes.HeatmapFilters()
  classes.get_bounding_box_for_location(filters)
  is_mobile = request.user_agent.platform in ["android", "iphone"] or \
              "Mobile" in request.user_agent.string

  if request.method == "POST":
    action = request.form.get("action")

    if action == "clear_values":
      filters.clear_values()

      return render_template(
        "heatmap.html",
        filters = filters,
        limit_reached = False,
        result_rows = 1,
        map_available = False
      )

    filters.read_from_form(request.form)

  conn = mysql.connector.connect(**db_config)
  cursor = conn.cursor(dictionary = True)

  basic_params = []
  visit_params = []
  where_clause = "WHERE 1=1 "
  having_clause = "HAVING visits >= %s"

  if filters.min_date:
    basic_params.append(filters.min_date)
    where_clause += f" AND t.date_taken >= %s"
  if filters.max_date:
    basic_params.append(filters.max_date)
    where_clause += f" AND t.date_taken <= %s"
  if filters.min_km is not None:
    basic_params.append(int(filters.min_km * 1000))
    where_clause += f" AND t.length >= %s"
  if filters.max_km is not None:
    basic_params.append(int(filters.max_km * 1000))
    where_clause += f" AND t.length <= %s"
  if filters.min_lat is not None:
    basic_params.append(filters.min_lat)
    where_clause += f" AND t.middle_lat >= %s"
  if filters.max_lat is not None:
    basic_params.append(filters.max_lat)
    where_clause += f" AND t.middle_lat <= %s"

  if filters.min_lon is not None:
    basic_params.append(filters.min_lon)
    where_clause += f" AND t.middle_lon >= %s"
  if filters.max_lon is not None:
    basic_params.append(filters.max_lon)
    where_clause += f" AND t.middle_lon <= %s"

  if filters.track_types:
    basic_params.extend(filters.track_types)
    placeholders = ",".join(["%s"] * len(filters.track_types))
    where_clause += f" AND t.type IN ({placeholders})"

  # there should always be min visits
  if filters.min_visits is None:
    filters.min_visits = 1
  visit_params.append(filters.min_visits)

  if filters.max_visits is not None:
    visit_params.append(filters.max_visits)
    having_clause += " AND visits <= %s"

  sql = f"""
    SELECT
    h.cell_id,
    t.type,
    t.name,
    t.date_taken,
    COUNT(*) AS visits

    FROM track_h3 h

    JOIN track t
    ON h.track_name=t.name
    
    {where_clause}

    GROUP BY h.cell_id
    {having_clause}
    LIMIT {filters.max_results + 1}
    """
  # logger.info(f"sql: {sql}")
  # logger.info(f"params: {basic_params + visit_params}")

  cursor.execute(sql, basic_params + visit_params)
  rows = cursor.fetchall()

  result_rows = len(rows)
  limit_reached = result_rows > filters.max_results
  if limit_reached:
    rows = rows[:filters.max_results]

  all_points = []

  m = folium.Map(
    tiles = "OpenStreetMap"
  )

  if rows:

    colormap = linear.Set1_09.to_step(9).scale(1, 10)

    for row in rows:
      cell_id = row["cell_id"]
      visits = row["visits"]

      boundary = h3.cell_to_boundary(cell_id)
      all_points.extend(boundary)
      color = colormap(visits)

      popup_text = get_tracks(cursor, cell_id, where_clause, basic_params)
      popup = folium.Popup(
        popup_text,
        max_width=300
      )

      polygon = folium.Polygon(
        locations = boundary,
        color = color,
        fill = True,
        fill_color = color,
        fill_opacity = 0.6,
        weight = 0,
        popup = popup
        )

      if not is_mobile:
        polygon.add_child(
          folium.Tooltip(f"{visits} visits<br /> <ul>{get_tracks(cursor, cell_id, where_clause, basic_params)}</ul>")
        )

      polygon.add_to(m)

    colormap.caption = "Number of visits"
    colormap.add_to(m)
    m.fit_bounds(all_points)
  m.save("static/maps/heatmap.html")

  cursor.close()
  conn.close()

  return render_template(
    "heatmap.html",
    map_available = True,
    filters=filters,
    limit_reached = limit_reached,
    result_rows = result_rows
  )

def get_show_photo_request(url: str) -> Response:
  r = requests.get(
    url,
    auth = (NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
    stream = True,
  )
  return r

def get_photo_info_request(url: str) -> Response:
  r = requests.get(
    url,
    auth = (NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
    verify = True,  # now properly fixed
  )
  return r


def get_nextcloud_url(dir, year, month, day, filename) -> str:
  base_path = f"{dir}/{year}/{month}/{day}"
  if filename:
    base_path = f"{base_path}/{filename}"
  url = (
      NEXTCLOUD_DAV_URL
      + base_path
  )
  return url


def get_photos_request(url: str) -> Response:
  headers = {"Depth": "1"}
  r = requests.request(
    "PROPFIND",
    url,
    headers = headers,
    auth = (NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
  )
  return r

# if __name__ == "__main__":
#     app.run(debug=True)
