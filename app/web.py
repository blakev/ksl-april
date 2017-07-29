#!/usr/bin/env python
# -*- coding: utf-8 -*-
# >>
#   Blake VandeMerwe
#   ksl-april, 2017
# <<

from gevent.monkey import patch_all
patch_all()

import daiquiri
from daiquiri import getLogger, setup
import datetime
from dotmap import DotMap
from flask import (
    Flask, jsonify, render_template, flash, redirect, abort, url_for)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
import gevent
from gevent.lock import Semaphore
import json
import logging
import os
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy import desc
import sys
from twilio.rest import Client
from urllib.parse import urlparse, parse_qs
from wtforms import StringField, SelectField
from wtforms.validators import (
    DataRequired, Length, URL, NumberRange, ValidationError)

# global constants
DEFAULT_TIMEOUT = (30.0, 15.0)
FOUND_TEMPLATE = "{} found: {} ({})"

# logging stuffs
setup(
  level=logging.INFO,
  outputs=[
      daiquiri.output.Stream(sys.stdout),
      daiquiri.output.TimedRotatingFile('application.log',
                                        level=logging.INFO)
  ]
)
logger = getLogger(__name__)


# flask stuffs
app = Flask(__name__)
app.config.from_json('../config.json')
db = SQLAlchemy(app)
config = DotMap(app.config)

# twilio
sms_config = config.EXTRA.twilio if not config.DEBUG else config.EXTRA.twilio_test
sms = Client(sms_config.account, sms_config.auth_token)

# setup selenium headless

def make_driver():
    d_path = os.path.abspath(os.path.join(os.path.split(__file__)[0], os.pardir, 'bin'))
    d_options = ChromeOptions()
    d_options.add_argument('--headless')
    d_options.add_argument('--window-size=1920,1080')
    # d_options.add_extension(os.path.join(d_path, 'ublock.crx'))
    d_options.binary_location = r'/usr/bin/chromium'
    driver = Chrome(os.path.join(d_path, 'chromedriver'), chrome_options=d_options)
    driver.implicitly_wait(DEFAULT_TIMEOUT[0])
    logger.info('created ChromeDriver %s', driver)
    return driver


class CRUDMixin(object):
    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modified = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Searches(CRUDMixin, db.Model):
    name = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(1024), nullable=False)
    every = db.Column(db.Integer, nullable=False, default=5)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    deleted = db.Column(db.Boolean, default=False, nullable=False)
    items = db.relationship('FoundItems', backref='search', lazy='dynamic')

    def __init__(self, name, url, every):
        self.name = name
        self.url = url
        self.every = every

    def __str__(self):
        return '<Search-%d(name=%s, every=%dmin)>' % \
               (self.id, self.name, self.every)

    @property
    def preview(self):
        d = parse_qs(urlparse(self.url).query)
        o = {}
        for k, v in d.items():
            k = k.lower()
            if k in config.EXTRA.skip_keys:
                continue
            if isinstance(v, list) and len(v) == 1:
                o[k] = v[0]
            else:
                o[k] = v
        return sorted(o.items())

    @property
    def last_found(self):
        return FoundItems.query\
                .filter_by(search_id=self.id)\
                .order_by(desc(FoundItems.created))\
                .first()

    @property
    def items_found(self):
        return FoundItems.query\
            .filter_by(search_id=self.id).count()


class FoundItems(CRUDMixin, db.Model):
    search_id = db.Column(db.Integer, db.ForeignKey('searches.id'))
    ksl_id = db.Column(db.Integer, nullable=False)
    ksl_title = db.Column(db.String(255), nullable=True)


class SearchForm(FlaskForm):
    name = StringField('Name', [DataRequired(), Length(max=255)],
                       description="Reference name for this search query.")

    url = StringField('URL', [DataRequired(), URL(), Length(max=1024)],
                      description="A valid KSL Cars search URL.")

    every = SelectField('Every', [DataRequired(), NumberRange(1, 60)],
                        coerce=int,
                        description="The search will be performed roughly on this time interval.",
                        choices=[
                            (2, '2 Minutes'),
                            (3, '3 Minutes'),
                            (5, '5 Minutes'),
                            (15, '15 Minutes'),
                            (30, '30 Minutes'),
                            (60, '1 Hour')])

    def validate_name(self, field):
        if Searches.query.filter_by(name=field.data).first():
            raise ValidationError('"%s" is already the name of a search.' % field.data)


db.create_all()


@app.route('/', methods=['GET', 'POST'])
def index():
    form = SearchForm()

    if form.validate_on_submit():
        o = Searches(form.name.data,
                     form.url.data,
                     form.every.data)
        db.session.add(o)
        db.session.commit()
        gevent.spawn(load_search, o.id, first=True)
        logger.info('created new search %s', o.id)
        flash('Successfully added new search <u>%s</u>' % o.name, 'success')

    searches = list(Searches.query.filter_by(enabled=True).all())
    disabled = list(Searches.query.filter_by(enabled=False, deleted=False).all())
    return render_template('index.html', form=form, searches=searches,
                           disabled_searches=disabled)

@app.route('/found/<int:search_id>')
def found_cars(search_id):
    search = Searches.query.filter_by(id=search_id).first()
    items = FoundItems.query.filter_by(search_id=search_id).order_by(desc(FoundItems.created)).all()
    return render_template('found.html', search=search, items=items)

@app.route('/search/<string:action>/<int:search_id>')
def modify_search(action, search_id):
    search = Searches.query.filter_by(id=search_id).first()
    if not search:
        logger.error('unknown search id: %s', search_id)
        abort(404)
    if action == 'enable':
        search.enabled = True
    elif action == 'disable':
        search.enabled = False
    elif action == 'delete':
        search.enabled = False
        search.deleted = True
    else:
        logger.error('unknown action: %s', action)
        abort(403)
    db.session.add(search)
    db.session.commit()
    logger.info('modified search %s, %s', search_id, action)
    flash('%sd search <u>%s</u>' % (action.capitalize(), search.name), 'info')
    return redirect(url_for('index'))


@app.route('/undelete-all', methods=['GET'])
def undelete_all():
    logger.info('undelete all')
    db.session.query(Searches).update({Searches.deleted: False})
    db.session.commit()
    flash('Undeleted all previous searches.', 'info')
    return redirect(url_for('index'))


@app.route('/stats', methods=['GET'])
def stats():
    return jsonify({})


def send_text_message(to, message):
    if not config.DEBUG:
        sms.messages.create(
            to=to,
            from_=config.EXTRA.twilio.phone,
            body=message
        )
    logger.info('sent text message to %s', to)


def get_title(d, url):
    d.get(url)
    title = d.title.split('|')[0].strip()
    return title


def load_search(s_id, *, first=False):
    s = Searches.query.filter_by(id=s_id).first()

    logger.info('starting %s, first? %s', s, first)

    driver = make_driver()
    # load the page
    driver.get(s.url)
    WebDriverWait(driver, 15, 0.25).until(lambda d: d.execute_script("return document.readyState") == 'complete')

    gevent.sleep(6)

    # scroll to the bottom of the page
    driver.execute_script('window.scrollTo(0, document.body.scrollHeight)')

    def find_listings(drv):
        ksl_ids = []
        el = drv.find_elements_by_css_selector('div.listing')
        if el:
            for e in el:
                ksl_ids.append(e.get_attribute('data-id'))
            return ksl_ids
        else:
            return False

    # get the listings
    all_items = WebDriverWait(driver, 20, 1).until(find_listings)
    logger.info('found %d items', len(all_items))

    # previously found
    for el in all_items:
        if el is None:
            continue

        if FoundItems.query.filter(FoundItems.ksl_id==el,
                                   FoundItems.search_id==s.id).count() == 0:
            # item does not exist
            f = FoundItems()
            f.ksl_id = el
            f.search_id = s.id

            logger.info('found -- %s:%s', f.ksl_id, f.search_id)

            if not first:
                # not found on startup, we need to do something with this item
                car_url = config.EXTRA.ksl_car_url + f.ksl_id
                f.ksl_title = get_title(driver, car_url)
                logger.info('new item: %s', f.ksl_title)

                if s.enabled:
                    send_text_message(config.EXTRA.phone_number,
                                      FOUND_TEMPLATE.format(s.name, f.ksl_title, car_url))
            db.session.add(f)
            db.session.commit()

    logger.info('done with %s', s)

    # queue it up for later
    gevent.spawn_later(s.every*60, load_search, s_id, first=False)

    driver.quit()


# initial search!!
threads = []
for s in Searches.query.filter_by(deleted=False).all():
    logger.info('loading search: %s', s.name)
    g = gevent.spawn(load_search, s.id, first=True)
    threads.append(g)
for t in threads:
    t.join()


#

if __name__ == '__main__':
    app.run('localhost', 5999)
    #
    # except KeyboardInterrupt:
    #     driver.quit()