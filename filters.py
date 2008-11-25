import datetime
from django.template.defaultfilters import timesince
from django.conf import settings
from google.appengine.ext import webapp

register = webapp.template.create_template_register()

UTC_OFFSET = getattr(settings, "UTC_OFFSET", 0)

def bettertimesince(dt):
    delta = datetime.datetime.utcnow() - dt
    local_dt = dt + datetime.timedelta(hours=UTC_OFFSET)
    if delta.days == 0:
        return timesince(dt) + " ago"
    elif delta.days == 1:
        return "Yesterday" + local_dt.strftime(" at %I:%M %p")
    elif delta.days < 5:
        return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][local_dt.weekday()] + local_dt.strftime(" at %I:%M %p")
    elif delta.days < 365:
        return local_dt.strftime("%B %d at %I:%M %p")
    else:
        return local_dt.strftime("%B %d, %Y")

register.filter(bettertimesince)
