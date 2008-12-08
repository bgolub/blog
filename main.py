import BeautifulSoup
import feedgenerator
import functools
import hashlib
import logging
import os
import uuid
import urllib

from django.conf import settings
settings._target = None
os.environ["DJANGO_SETTINGS_MODULE"] = "settings"

from django import newforms as forms
from django.template.defaultfilters import slugify
from django.utils import simplejson
from django.utils.feedgenerator import Enclosure

from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.db import djangoforms
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

DOPPLR_TOKEN = getattr(settings, "DOPPLR_TOKEN", None)
MAPS_API_KEY = getattr(settings, "MAPS_API_KEY", None)
SHOW_CURRENT_CITY = getattr(settings, "SHOW_CURRENT_CITY", False)
TITLE = getattr(settings, "TITLE", "Blog")
OLD_WORDPRESS_BLOG = getattr(settings, "OLD_WORDPRESS_BLOG", None)

webapp.template.register_template_library("filters")

def admin(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        user = users.get_current_user()
        if not user:
            if self.request.method == "GET":
                return self.redirect(users.create_login_url(self.request.uri))
            return self.error(403)
        elif not users.is_current_user_admin():
            return self.error(403)
        else:
            return method(self, *args, **kwargs)
    return wrapper


class MediaRSSFeed(feedgenerator.Atom1Feed):
    def root_attributes(self):
        attrs = super(MediaRSSFeed, self).root_attributes()
        attrs["xmlns:media"] = "http://search.yahoo.com/mrss/"
        return attrs

    def add_item_elements(self, handler, item):
        super(MediaRSSFeed, self).add_item_elements(handler, item)
        self.add_thumbnail_element(handler, item)

    def add_thumbnail_element(self, handler, item):
        thumbnail = item.get("thumbnail", None)
        if thumbnail:
            title = thumbnail.get("title", None)
            if title:
                handler.addQuickElement("media:title", title)
            handler.addQuickElement("media:thumbnail", "", {
                "url": thumbnail["url"],
            })


class Entry(db.Model):
    author = db.UserProperty()
    title = db.StringProperty(required=True)
    slug = db.StringProperty(required=True)
    body = db.TextProperty(required=True)
    published = db.DateTimeProperty(auto_now_add=True)
    updated = db.DateTimeProperty(auto_now=True)
    tags = db.ListProperty(db.Category)


class EntryForm(djangoforms.ModelForm):
    class Meta:
        model = Entry
        exclude = ["author", "slug", "published", "updated", "tags"]


class BaseRequestHandler(webapp.RequestHandler):
    def head(self):
        pass

    def raise_error(self, code):
        self.error(code)
        self.render("%i.html" % code)

    def get_current_city(self):
        key = "current_city/now"
        current_city = memcache.get(key)
        if not current_city:
            response = urlfetch.fetch("https://www.dopplr.com/api/traveller_info?format=js&token=" + DOPPLR_TOKEN)
            if response.status_code == 200:
                data = simplejson.loads(response.content)
                current_city = data["traveller"]["current_city"]
                current_city["maps_api_key"] = MAPS_API_KEY
                memcache.set(key, current_city, 60*60*5)
            else:
                current_city = None
        return current_city

    def get_recent_entries(self):
        key = "entries/recent"
        entries = memcache.get(key)
        if not entries:
            entries = db.Query(Entry).order("-published").fetch(limit=5)
            memcache.set(key, list(entries))
        return entries

    def get_main_page_entries(self):
        key = "entries/main"
        entries = memcache.get(key)
        if not entries:
            entries = db.Query(Entry).order("-published").fetch(limit=10)
            memcache.set(key, list(entries))
        return entries

    def get_archive_entries(self):
        key = "entries/archive"
        entries = memcache.get(key)
        if not entries:
            entries = db.Query(Entry).order("-published")
            memcache.set(key, list(entries))
        return entries

    def get_entry_from_slug(self, slug):
        key = "entry/%s" % slug
        entry = memcache.get(key)
        if not entry:
            entry = db.Query(Entry).filter("slug =", slug).get()
            if entry:
                memcache.set(key, entry)
        return entry

    def get_tagged_entries(self, tag):
        key = "entries/tag/%s" % tag
        entries = memcache.get(key)
        if not entries:
            entries = db.Query(Entry).filter("tags =", tag).order("-published")
            memcache.set(key, list(entries))
        return entries

    def kill_entries_cache(self, slug=None, tags=[]):
        memcache.delete("entries/recent")
        memcache.delete("entries/main")
        memcache.delete("entries/archive")
        if slug:
            memcache.delete("entry/%s" % slug)
        for tag in tags:
            memcache.delete("entries/tag/%s" % tag)
        
    def get_integer_argument(self, name, default):
        try:
            return int(self.request.get(name, default))
        except (TypeError, ValueError):
            return default

    def fetch_headers(self, url):
        key = "headers/" + url
        headers = memcache.get(key)
        if not headers:
            response = urlfetch.fetch(url, method=urlfetch.HEAD)
            if response.status_code == 200:
                headers = response.headers
                memcache.set(key, headers)
        return headers

    def find_enclosure(self, html):
        soup = BeautifulSoup.BeautifulSoup(html)
        img = soup.find("img")
        if img:
            headers = self.fetch_headers(img["src"])
            if headers:
                enclosure = Enclosure(img["src"], headers["Content-Length"],
                    headers["Content-Type"])
                return enclosure
        return None

    def find_thumbnail(self, html):
        soup = BeautifulSoup.BeautifulSoup(html)
        img = soup.find("img")
        if img:
            return {
                "url": img["src"],
                "title": img.get("title", img.get("alt", None)),
            }
        return None

    def generate_sup_id(self, url=None):
        return hashlib.md5(url or self.request.url).hexdigest()[:10]

    def set_sup_id_header(self):
        sup_id = self.generate_sup_id()
        self.response.headers["X-SUP-ID"] = \
            "http://friendfeed.com/api/public-sup.json#%s" % sup_id
            
    def render_feed(self, entries):
        f = MediaRSSFeed(
            title=TITLE,
            link="http://" + self.request.host + "/",
            description=TITLE,
            language="en",
        )
        for entry in entries[:10]:
            f.add_item(
                title=entry.title,
                link=self.entry_link(entry, absolute=True),
                description=entry.body,
                author_name=entry.author.nickname(),
                pubdate=entry.published,
                categories=entry.tags,
                thumbnail=self.find_thumbnail(entry.body),
            )
        data = f.writeString("utf-8")
        self.response.headers["Content-Type"] = "application/atom+xml"
        self.set_sup_id_header()
        self.response.out.write(data)

    def render_json(self, entries):
        json_entries = [{
            "title": entry.title,
            "slug": entry.slug,
            "body": entry.body,
            "author": entry.author.nickname(),
            "published": entry.published.isoformat(),
            "updated": entry.updated.isoformat(),
            "tags": entry.tags,
            "link": self.entry_link(entry, absolute=True),
        } for entry in entries]
        json = {"entries": json_entries}
        self.response.headers["Content-Type"] = "text/javascript"
        self.response.out.write(simplejson.dumps(json, sort_keys=True, 
            indent=self.get_integer_argument("pretty", None)))

    def render(self, template_file, extra_context={}):
        if "entries" in extra_context:
            format = self.request.get("format", None)
            if format == "atom":
                return self.render_feed(extra_context["entries"])
            elif format == "json":
                return self.render_json(extra_context["entries"])
        extra_context["request"] = self.request
        extra_context["admin"] = users.is_current_user_admin()
        extra_context["recent_entries"] = self.get_recent_entries()
        if SHOW_CURRENT_CITY:
            extra_context["current_city"] = self.get_current_city()
        extra_context.update(settings._target.__dict__)
        template_file = "templates/%s" % template_file
        path = os.path.join(os.path.dirname(__file__), template_file)
        self.response.out.write(template.render(path, extra_context))

    def ping(self, entry=None):
        feed = "http://" + self.request.host + "/?format=atom"
        args = urllib.urlencode({
            "name": TITLE,
            "url": "http://" + self.request.host + "/",
            "changesURL": feed,
        })
        response = urlfetch.fetch("http://blogsearch.google.com/ping?" + args)
        args = urllib.urlencode({
            "url": feed,
            "supid": self.generate_sup_id(feed),
        })
        response = urlfetch.fetch("http://friendfeed.com/api/public-sup-ping?" \
            + args)

    def is_valid_xhtml(self, entry):
        args = urllib.urlencode({
            "uri": self.entry_link(entry, absolute=True),
        })
        response = urlfetch.fetch("http://validator.w3.org/check?" + args,
            method=urlfetch.HEAD)
        return response.headers["X-W3C-Validator-Status"] == "Valid"

    def entry_link(self, entry, query_args={}, absolute=False):
        url = "/e/" + entry.slug
        if absolute:
            url = "http://" + self.request.host + url
        if query_args:
            url += "?" + urllib.urlencode(query_args)
        return url


class ArchivePageHandler(BaseRequestHandler):
    def get(self):
        extra_context = {
            "entries": self.get_archive_entries(),
        }
        self.render("archive.html", extra_context)


class DeleteEntryHandler(BaseRequestHandler):
    @admin
    def post(self):
        key = self.request.get("key")
        try:
            entry = db.get(key)
            entry.delete()
            self.kill_entries_cache(slug=entry.slug, tags=entry.tags)
            data = {"success": True}
        except db.BadKeyError:
            data = {"success": False}
        json = simplejson.dumps(data)
        self.response.out.write(json)


class EntryPageHandler(BaseRequestHandler):
    def head(self, slug):
        entry = self.get_entry_from_slug(slug=slug)
        if not entry:
            self.error(404)

    def get(self, slug):
        entry = self.get_entry_from_slug(slug=slug)
        if not entry:
            return self.raise_error(404)
        extra_context = {
            "entries": [entry], # So we can use the same template for everything
            "entry": entry, # To easily pull out the title
            "previous": db.Query(Entry).filter("published <", entry.published).order("-published").get(),
            "next": db.Query(Entry).filter("published >", entry.published).order("published").get(),
            "invalid": self.request.get("invalid", False),
        }
        self.render("entry.html", extra_context)


class FeedRedirectHandler(BaseRequestHandler):
    def get(self):
        self.redirect("/?format=atom", permanent=True)


class MainPageHandler(BaseRequestHandler):
    def head(self):
        if self.request.get("format", None) == "atom":
            self.set_sup_id_header()

    def get(self):
        offset = self.get_integer_argument("start", 0)
        if not offset:
            entries = self.get_main_page_entries()
        else:
            entries = db.Query(Entry).order("-published").fetch(limit=10, offset=offset)
        if not entries and offset > 0:
            return self.redirect("/")
        extra_context = {
            "entries": entries,
            "next": max(offset - 10, 0),
            "previous": offset + 10 if len(entries) == 10 else None,
            "offset": offset,
        }
        self.render("main.html", extra_context)


class NewEntryHandler(BaseRequestHandler):
    def get_tags_argument(self, name):
        tags = [slugify(tag) for tag in self.request.get(name, "").split(",")]
        tags = set([tag for tag in tags if tag])
        return [db.Category(tag) for tag in tags]
    
    @admin
    def get(self, key=None):
        extra_context = {}
        form = EntryForm()
        if key:
            try:
                entry = db.get(key)
                extra_context["entry"] = entry
                extra_context["tags"] = ", ".join(entry.tags)
                form = EntryForm(instance=entry)
            except db.BadKeyError:
                return self.redirect("/new")
        extra_context["form"] = form
        self.render("edit.html" if key else "new.html", extra_context)

    @admin
    def post(self, key=None):
        extra_context = {}
        form = EntryForm(data=self.request.POST)
        if form.is_valid():
            if key:
                try:
                    entry = db.get(key)
                    extra_context["entry"] = entry
                except db.BadKeyError:
                    return self.raise_error(404)
                entry.title = self.request.get("title")
                entry.body = self.request.get("body")
            else:
                slug = slugify(self.request.get("title"))
                if self.get_entry_from_slug(slug=slug):
                    slug += "-" + uuid.uuid4().hex[:4]
                entry = Entry(
                    author=users.get_current_user(),
                    body=self.request.get("body"),
                    title=self.request.get("title"),
                    slug=slug,
                )
            entry.tags = self.get_tags_argument("tags")
            entry.put()
            self.kill_entries_cache(slug=entry.slug if key else None,
                tags=entry.tags)
            if not key:
                self.ping(entry)
            valid = self.is_valid_xhtml(entry)
            return self.redirect(self.entry_link(entry,
                query_args={"invalid": 1} if not valid else {}))
        extra_context["form"] = form
        self.render("edit.html" if key else "new.html", extra_context)


class NotFoundHandler(BaseRequestHandler):
    def head(self):
        self.error(404)

    def get(self):
        self.raise_error(404)


class OldBlogRedirectHandler(BaseRequestHandler):
    def get(self, year, month, day, slug):
        if not OLD_WORDPRESS_BLOG:
           return self.raise_error(404) 
        self.redirect("http://%s/%s/%s/%s/%s/" % 
            (OLD_WORDPRESS_BLOG, year, month, day, slug), permanent=True)


class TagPageHandler(BaseRequestHandler):
    def get(self, tag):
        extra_context = {
            "entries": self.get_tagged_entries(tag),
            "tag": tag,
        }
        self.render("tag.html", extra_context)


class OpenSearchHandler(BaseRequestHandler):
    def get(self):
        self.response.headers["Content-Type"] = "application/xml"
        self.render("opensearch.xml")


application = webapp.WSGIApplication([
    ("/", MainPageHandler),
    ("/archive/?", ArchivePageHandler),
    ("/delete/?", DeleteEntryHandler),
    ("/edit/([\w-]+)/?", NewEntryHandler),
    ("/e/([\w-]+)/?", EntryPageHandler),
    ("/new/?", NewEntryHandler),
    ("/t/([\w-]+)/?", TagPageHandler),
    ("/(\d+)/(\d+)/(\d+)/([\w-]+)/?", OldBlogRedirectHandler),
    ("/feed/?", FeedRedirectHandler),
    ("/opensearch.xml/?", OpenSearchHandler),
    ("/.*", NotFoundHandler),
], debug=True)

def main():
    logging.getLogger().setLevel(logging.DEBUG)
    run_wsgi_app(application)

if __name__ == "__main__":
    main()
