#!/usr/env python
# -*- coding: utf-8 -*-
# This file is part of everylotbot
# Copyright 2016 Neil Freeman
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import logging
import math
import sqlite3
from io import BytesIO

import googlemaps
import googlemaps.maps
import requests
import shapely.geometry
import shapely.wkb

QUERY = """SELECT
    *
    FROM lots
    where {} = ?
    ORDER BY id ASC
    LIMIT 1;
"""

SVAPI = "https://maps.googleapis.com/maps/api/streetview"
GCAPI = "https://maps.googleapis.com/maps/api/geocode/json"


class EveryLot(object):

    def __init__(self, database,
                 search_format=None,
                 print_format=None,
                 id_=None,
                 **kwargs):
        """
        An everylot class immediately checks the database for the next available entry,
        or for the passed 'id_'. It stores this data in self.lot.
        :database str file name of database
        """
        self.logger = kwargs.get('logger', logging.getLogger('everylot'))

        # set address format for fetching from DB
        self.search_format = search_format or '{address}, {city} {state}'
        self.print_format = print_format or '{address}'

        self.logger.debug('searching google sv with %s', self.search_format)
        self.logger.debug('posting with %s', self.print_format)

        self.conn = sqlite3.connect(database)

        if id_:
            field = 'id'
            value = id_
        else:
            field = 'tweeted'
            value = 0

        curs = self.conn.execute(QUERY.format(field), (value,))
        keys = [c[0] for c in curs.description]
        self.lot = dict(zip(keys, curs.fetchone()))

    def aim_camera(self):
        '''Set field-of-view and pitch'''
        fov, pitch = 65, 10
        try:
            floors = float(self.lot.get('floors', 0)) or 2
        except TypeError:
            floors = 2

        if floors == 3:
            fov = 72

        if floors == 4:
            fov, pitch = 76, 15

        if floors >= 5:
            fov, pitch = 81, 20

        if floors == 6:
            fov = 86

        if floors >= 8:
            fov, pitch = 90, 25

        if floors >= 10:
            fov, pitch = 90, 30

        return fov, pitch

    def get_streetview_image(self, key):
        '''Fetch image from streetview API'''
        params = {
            "location": self.streetviewable_location(key),
            "key": key,
            "size": "1000x1000"
        }

        params['fov'], params['pitch'] = self.aim_camera()

        r = requests.get(SVAPI, params=params)
        self.logger.debug(r.url)

        sv = BytesIO()
        for chunk in r.iter_content():
            sv.write(chunk)

        sv.seek(0)
        return sv

    def get_maps_image(self, key):
        client = googlemaps.Client(key)

        shape = shapely.wkb.loads(self.lot["geometry"])
        parcel = shapely.geometry.mapping(shape)

        if parcel["type"] == "Polygon":
            polygons = parcel["coordinates"]
        elif parcel["type"] == "MultiPolygon":
            polygons = [
                polygon[0] for polygon in parcel["coordinates"]
            ]
        else:
            raise ValueError(f"Unknown geometry type: {parcel['type']}")

        paths = [
            googlemaps.maps.StaticMapPath(points=[{"lat": lat, "lng": lng} for lng, lat in polygon])
            for polygon in polygons
        ]
        bounds = self.scale_bounds(shape.bounds, 3)
        zoom = self.calculate_zoom(bounds, [1000, 1000])
        resp = client.static_map(
            size=1000,
            center={"lat": shape.centroid.y, "lng": shape.centroid.x},
            path=paths,
            maptype="roadmap",
            zoom=zoom,
        )

        im = BytesIO()
        for chunk in resp:
            if chunk:
                im.write(chunk)
        im.seek(0)
        return im

    def scale_bounds(self, bounds, scale_factor):
        center = [
            (bounds[2] + bounds[0]) / 2,
            (bounds[3] + bounds[1]) / 2,
        ]
        dimensions = [
            bounds[2] - bounds[0],
            bounds[3] - bounds[1],
        ]
        return [
            center[0] - dimensions[0] * scale_factor / 2,
            center[1] - dimensions[1] * scale_factor / 2,
            center[0] + dimensions[0] * scale_factor / 2,
            center[1] + dimensions[1] * scale_factor / 2,
        ]

    def calculate_zoom(self, bounds, mapDim):
        """Adapted from https://stackoverflow.com/a/13274361."""
        WORLD_DIM = { "height": 256, "width": 256 }
        ZOOM_MAX = 20

        def latRad(lat):
            sin = math.sin(lat * math.pi / 180)
            radX2 = math.log((1 + sin) / (1 - sin)) / 2
            return max(min(radX2, math.pi), -math.pi) / 2

        def zoom(mapPx, worldPx, fraction):
            return math.floor(math.log(mapPx / worldPx / fraction) / math.log(2))

        latFraction = (latRad(bounds[3]) - latRad(bounds[1])) / math.pi

        lngDiff = bounds[2] - bounds[0]
        lngFraction = (lngDiff + 360 if lngDiff < 0 else lngDiff) / 360

        latZoom = zoom(mapDim[1], WORLD_DIM["height"], latFraction)
        lngZoom = zoom(mapDim[0], WORLD_DIM["width"], lngFraction)

        return min(latZoom, lngZoom, ZOOM_MAX)

    def streetviewable_location(self, key):
        '''
        Check if google-geocoded address is nearby or not. if not, use the lat/lon
        '''
        # skip this step if there's no address, we'll just use the lat/lon to fetch the SV.
        try:
            address = self.search_format.format(**self.lot)

        except KeyError:
            self.logger.warn('Could not find street address, using lat/lon')
            return '{},{}'.format(self.lot['lat'], self.lot['lon'])

        # bounds in (miny minx maxy maxx) aka (s w n e)
        try:
            d = 0.007
            minpt = self.lot['lat'] - d, self.lot['lon'] - d
            maxpt = self.lot['lat'] + d, self.lot['lon'] + d

        except KeyError:
            self.logger.info('No lat/lon coordinates. Using address naively.')
            return address

        params = {
            "address": address,
            "key": key,
        }

        self.logger.debug('geocoding @ google')

        try:
            r = requests.get(GCAPI, params=params)
            self.logger.debug(r.url)

            if r.status_code != 200:
                raise ValueError('bad response from google geocode: %s' % r.status_code)

            loc = r.json()['results'][0]['geometry']['location']

            # Cry foul if we're outside of the bounding box
            outside_comfort_zone = any((
                loc['lng'] < minpt[1],
                loc['lng'] > maxpt[1],
                loc['lat'] > maxpt[0],
                loc['lat'] < minpt[0]
            ))

            if outside_comfort_zone:
                raise ValueError('google geocode puts us outside outside our comfort zone')

            self.logger.debug('using db address for sv')
            return address

        except Exception as e:
            self.logger.info(e)
            self.logger.info('location with db coords: %s, %s', self.lot['lat'], self.lot['lon'])
            return '{},{}'.format(self.lot['lat'], self.lot['lon'])

    def compose(self, media_ids):
        '''
        Compose a tweet, including media ids and location info.
        :media_id_string str identifier for an image uploaded to Twitter
        '''
        self.logger.debug("media_ids: %s", media_ids)

        # Let missing addresses play through here, let the program leak out a bit
        status = self.print_format.format(**self.lot)

        return {
            "status": status,
            "lat": self.lot.get('lat', 0.),
            "long": self.lot.get('lon', 0.),
            "media_ids": media_ids,
        }

    def mark_as_tweeted(self, status_id):
        self.conn.execute("UPDATE lots SET tweeted = ? WHERE id = ?", (status_id, self.lot['id'],))
        self.conn.commit()
