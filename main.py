import functools
import os

os.environ['DJANGO_SETTINGS_MODULE'] = 'settings'

from django.template.defaultfilters import slugify
from django.utils import feedgenerator
from django.utils import simplejson

from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.db import djangoforms
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

def admin(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        user = users.get_current_user()
        if not user:
            if self.request.method == 'GET':
                return self.redirect(users.create_login_url(self.request.uri))
            return self.error(403)
        elif not users.is_current_user_admin():
            return self.error(403)
        else:
            return method(self, *args, **kwargs)
    return wrapper


class Entry(db.Model):
    author = db.UserProperty()
    title = db.StringProperty(required=True)
    slug = db.StringProperty(required=True)
    body = db.TextProperty(required=True)
    published = db.DateTimeProperty(auto_now_add=True)
    updated = db.DateTimeProperty(auto_now=True)


class EntryForm(djangoforms.ModelForm):
    class Meta:
        model = Entry
        exclude = ['author', 'slug', 'published', 'updated']


class BaseRequestHandler(webapp.RequestHandler):
    def get_integer_argument(self, name, default):
        try:
            return int(self.request.get(name, default))
        except ValueError:
            return default


    def render_feed(self, entries):
        f = feedgenerator.Atom1Feed(
            title = 'Benjamin Golub\'s Blog',
            link = 'http://' + self.request.host,
            description = 'Benjamin Golub\'s Blog',
            language = 'en',
        )
        for entry in entries:
            f.add_item(
                title = entry.title,
                link = '/e/%s' % entry.slug,
                description = entry.body,
                author_name = entry.author.nickname(),
                pubdate = entry.published,
            )
        data = f.writeString('utf-8')
        self.response.out.write(data)


    def render(self, template_file, extra_context={}):
        extra_context['request'] = self.request
        extra_context['admin'] = users.is_current_user_admin()
        path = os.path.join(os.path.dirname(__file__), template_file)
        self.response.out.write(template.render(path, extra_context))


class ArchivePageHandler(BaseRequestHandler):
    def get(self):
        entries = db.Query(Entry).order('-published')
        extra_context = {
            'entries': entries,
        }
        self.render('archive.html', extra_context)


class DeleteEntryHandler(BaseRequestHandler):
    @admin
    def post(self):
        key = self.request.get('key')
        try:
            entry = db.get(key)
            entry.delete()
        except db.BadKeyError:
            data = {"success": False}
        data = {"success": True}
        json = simplejson.dumps(data)
        return self.response.out.write(json)


class EntryPageHandler(BaseRequestHandler):
    def get(self, slug):
        entry = db.Query(Entry).filter('slug =', slug).get()
        if not entry:
            return self.error(404)
        extra_context = {
            'entries': [entry], # So we can use the same template for everything
            'entry': entry, # To easily pull out the title
        }
        previous = db.Query(Entry).filter('published <', entry.published).order('-published').get()
        if previous:
            extra_context["previous"] = previous
        next = db.Query(Entry).filter('published >', entry.published).order('published').get()
        if next:
            extra_context["next"] = next
        self.render('entry.html', extra_context)


class MainPageHandler(BaseRequestHandler):
    def get(self):
        offset = self.get_integer_argument('start', 0)
        entries = db.Query(Entry).order('-published').fetch(limit=10, offset=offset)
        if not entries and offset > 0:
            return self.redirect('/')
        if self.request.get('format', None) == 'atom':
            return self.render_feed(entries)
        extra_context = {
            'entries': entries,
            'next': max(offset - 10, 0),
            'previous': offset + 10,
        }
        self.render('main.html', extra_context)


class NewEntryHandler(BaseRequestHandler):
    @admin
    def get(self, key=None):
        extra_context = {}
        form = EntryForm()
        if key:
            try:
                entry = db.get(key)
                form = EntryForm(instance=entry)
            except db.BadKeyError:
                return self.redirect("/new")
        extra_context['form'] = form
        self.render('new.html', extra_context)

    @admin
    def post(self, key=None):
        form = EntryForm(data=self.request.POST)
        if form.is_valid():
            if key:
                try:
                    entry = db.get(key)
                    entry.body = self.request.get('body')
                    entry.title = self.request.get('title')
                    entry.put()
                    return self.redirect('/e/' + entry.slug)
                except db.BadKeyError:
                    pass
            else:
                entry = Entry(
                    author=users.get_current_user(),
                    body=self.request.get('body'),
                    title=self.request.get('title'),
                    slug=slugify(self.request.get('title'))
                )
                entry.put()
                return self.redirect('/e/' + entry.slug)
        extra_context = {
            'form': form,
        }
        self.render('new.html', extra_context)
        

application = webapp.WSGIApplication([
    ('/', MainPageHandler),
    ('/archive', ArchivePageHandler),
    ('/delete', DeleteEntryHandler),
    ('/edit/([\w-]+)', NewEntryHandler),
    ('/e/([\w-]+)', EntryPageHandler),
    ('/new', NewEntryHandler),
], debug=True)

def main():
    run_wsgi_app(application)

if __name__ == '__main__':
    main()
