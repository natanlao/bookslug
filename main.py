# -*- coding: utf-8 -*-
from bs4 import BeautifulSoup
import peewee
from urlparse import parse_qs
import phpserialize
import urllib2
import re
import time
import datetime
import os
import logging
from flask import Flask, flash, redirect, render_template, request, url_for
from google.appengine.api import memcache, users
import isbnlib


log = logging.getLogger(__name__)
log.setLevel("DEBUG")

app = Flask(__name__)

DB_NAME = "bookworm.db"
# db = peewee.SqliteDatabase(DB_NAME)

CLOUDSQL_CONNECTION_NAME = os.environ.get('CLOUDSQL_CONNECTION_NAME')
CLOUDSQL_USER = os.environ.get('CLOUDSQL_USER')
CLOUDSQL_PASSWORD = os.environ.get('CLOUDSQL_PASSWORD')

if (os.getenv('SERVER_SOFTWARE') and os.getenv('SERVER_SOFTWARE').startswith('Google App Engine/')):
    cloudsql_unix_socket = os.path.join('/cloudsql', CLOUDSQL_CONNECTION_NAME)
    # db = peewee.MySQLDatabase("bookslug", host="104.198.34.59", unix_socket=cloudsql_unix_socket, user=CLOUDSQL_USER, passwd=CLOUDSQL_PASSWORD)
    # app.config['SQLALCHEMY_DATABASE_URI'] = ''mysql://dayenu:secret.word@localhost/dayenu?unix_socket=/usr/local/mysql5/mysqld.sock
    db = peewee.MySQLDatabase("mysql://%s:%s@%s/%s?unix_socket=%s" % (CLOUDSQL_USER, CLOUDSQL_PASSWORD, "", "bookslug", cloudsql_unix_socket))

else:
    db = peewee.MySQLDatabase("bookslug", user=CLOUDSQL_USER, passwd=CLOUDSQL_PASSWORD)

days = [
    ('sun', 'Sn'),
    ('mon', 'M'),
    ('tue', 'Tu'),
    ('wed', 'W'),
    ('thr', 'Th'),
    ('fri', 'F'),
    ('sat', 'St')
]


@app.template_filter('prettydate')
def prettydate(courseobj):
    datestring = ""
    for day_index, day_abbr in days:
        if courseobj.__dict__['_data'][day_index]:
            datestring += day_abbr

    if courseobj.start_time:
        start_time = courseobj.start_time.strftime("%I:%M %p").encode('utf-8')
        end_time = courseobj.end_time.strftime("%I:%M %p").encode('utf-8')
        datestring2 = " %s %s" % (start_time, end_time)
        datestring += datestring2

    return datestring


class Course(peewee.Model):
    # Course Description
    classnum = peewee.IntegerField()
    name = peewee.CharField()
    description = peewee.CharField()
    section = peewee.SmallIntegerField()

    # TODO: Create Instructor table
    # Instructor ID
    instructor_first = peewee.CharField(null=True)
    instructor_last = peewee.CharField(null=True)
    instructor_mid = peewee.CharField(null=True)

    location = peewee.CharField(null=True)
    slug = peewee.TextField()

    # Course scheduling
    sun = peewee.BooleanField()
    mon = peewee.BooleanField()
    tue = peewee.BooleanField()
    wed = peewee.BooleanField()
    thr = peewee.BooleanField()
    fri = peewee.BooleanField()
    sat = peewee.BooleanField()
    start_time = peewee.TimeField(null=True)
    end_time = peewee.TimeField(null=True)

    # Enrollment statistics
    # UCSC doesn't even have 30k students, so using a smallint *should* be okay
    capacity = peewee.SmallIntegerField()
    enrolled = peewee.SmallIntegerField()
    waitlist = peewee.SmallIntegerField()

    class Meta:
        database = db


class Textbook(peewee.Model):
    course = peewee.ForeignKeyField(Course)
    isbn = peewee.BigIntegerField()

    class Meta:
        database = db


class Student(peewee.Model):
    user_id = peewee.CharField(primary_key=True)

    class Meta:
        database = db


class StudentCourse(peewee.Model):
    student = peewee.ForeignKeyField(Student)
    course = peewee.ForeignKeyField(Course)

    class Meta:
        database = db


class Listing(peewee.Model):
    buyer = peewee.ForeignKeyField(Student, null=True, related_name="buyer")
    seller = peewee.ForeignKeyField(Student, null=True, related_name="seller")
    textbook = peewee.ForeignKeyField(Textbook)
    fulfilled = peewee.BooleanField(default=False)

    class Meta:
        database = db


def create_tables():
    return db.create_tables([Course, Textbook, Student, StudentCourse, Listing])


def parse_time(timestring):
    if timestring:
        # frick mysql
        return datetime.datetime.strptime(timestring, "%I:%M%p").strftime("%H:%M")
    return None


# http://blog.gregburek.com/2011/12/05/Rate-limiting-with-decorators/
def limit_rate(interval):
    def decorate(func):
        lastTimeCalled = [0.0]

        def rateLimitedFunction(*args, **kargs):
            elapsed = time.clock() - lastTimeCalled[0]
            leftToWait = interval - elapsed
            if leftToWait > 0:
                time.sleep(leftToWait)
            ret = func(*args, **kargs)
            lastTimeCalled[0] = time.clock()
            return ret

        return rateLimitedFunction

    return decorate


def parse_classdata(classdata, href):
    """Takes a Pisa class_data argument and parses it into a dictionary in
    the form of :class:`Course`."""
    data = phpserialize.loads(classdata.decode('base64'))

    attributes = {
        'classnum': data['CLASS_NBR'],
        'name': data['SUBJECT'] + " " + data['CATALOG_NBR'].strip(),
        'description': data['DESCR'],
        'section': int(data['CLASS_SECTION']),
        'instructor_first': data['FIRST_NAME'],
        'instructor_last': data['LAST_NAME'],
        'instructor_mid': data['MIDDLE_NAME'],
        'location': data['FAC_DESCR'],
        'slug': href,
        'sun': data['SUN'],
        'mon': data['MON'],
        'tue': data['TUES'],
        'wed': data['WED'],
        'thr': data['THURS'],
        'fri': data['FRI'],
        'sat': data['SAT'],
        'start_time': parse_time(data['START_TIME']),
        'end_time': parse_time(data['END_TIME']),
        'capacity': data['ENRL_CAP'],
        'enrolled': data['ENRL_TOT'],
        'waitlist': data['WAIT_TOT']
    }

    return attributes


def parse_html(html):
    """Takes content of a dump of the schedule of classes from Pisa."""
    s = BeautifulSoup(html, "html.parser")
    # Get hrefs of each course's details page (it has a lot of data in it
    # encoded in base64 and it's super cool!)
    links = [e.h2.a['href'] for e in s.find_all("div", class_="panel-heading-custom")]

    courses = []
    for link in links:
        classdata = parse_qs(link)['class_data'][0]
        courses.append(parse_classdata(classdata, link))

    with db.atomic():
        Course.insert_many(courses).execute()


@app.route('/textbooks/<course_id>')
def get_textbooks(course_id):
    course = Course.get(Course.id == course_id)
    link = "https://pisa.ucsc.edu/class_search/" + course.slug
    isbns = get_isbns(link)

    textbooks = memcache.get(course_id)
    if not textbooks:
        textbooks = []
        for isbn in isbns:
            Textbook.get_or_create(course=course.id, isbn=isbn)
            textbooks.append(isbnlib.meta(str(isbn)))
        memcache.set(course_id, textbooks, time=60 * 60 * 24 * 365)

    return render_template("textbooks.html", textbooks=textbooks)


@app.route('/textbooks/get/<book_isbn>')
def buy_book(book_isbn):
    textbook = Textbook.get(Textbook.isbn == book_isbn)
    Listing.create(buyer=users.get_current_user().user_id(), textbook=textbook.id)
    return redirect(url_for('courses_view'))


@app.route('/textbooks/sell/<book_isbn>')
def sell_book(book_isbn):
    textbook = Textbook.get(Textbook.isbn == book_isbn)
    Listing.create(seller=users.get_current_user().user_id(), textbook=textbook.id)
    return redirect(url_for('courses_view'))


@app.route('/listings/')
def show_listings():
    user = Student.get(Student.user_id == users.get_current_user().user_id())
    listings = Listing.select((Listing.buyer == user) | (Listing.seller == user))
    return render_template("listings.html", listings=listings)


@limit_rate(10)
def get_isbns(url):
    isbns = set()  # ISBNs can be listed multiple times in the same page - ensure no repeats
    isbn_pattern = r'"isbn":"((\\"|[^"]|)*)"'  # Matches isbn
    r = urllib2.urlopen(url)  # Open details page and get materials page
    s = BeautifulSoup(r.read(), "html.parser")
    n = s.find_all("div", class_="hide-print")[0].a['href']
    b = urllib2.urlopen(n)  # Open materials page and find all ISBNs
    for i, _ in re.findall(isbn_pattern, b.read()):
        isbns.add(int(i))

    return isbns


@app.errorhandler(500)
def server_error(e):
    # Log the error and stacktrace.
    logging.exception('An error occurred during a request.')
    return 'An internal error occurred.', 500


@app.route('/')
def index():
    if users.get_current_user():
        return redirect(url_for('courses_view'))
    return render_template("index.html")


@app.route('/courses')
def courses_view():
    if not Course.select().join(StudentCourse).join(Student).where(Student.user_id == users.get_current_user().user_id()):
        return redirect(url_for('add_courses'))
    return render_template("courses.html", courses=Course.select().join(StudentCourse).join(Student).where(Student.user_id == users.get_current_user().user_id()))


@app.route('/courses/add', methods=['GET', 'POST'])
def add_courses():
    if request.method == 'GET':
        return render_template("add_course.html")
    elif request.method == 'POST':
        StudentCourse.create(student=Student.get(Student.user_id == users.get_current_user().user_id()), course=Course.get(Course.name == request.form['course']))
        return redirect(url_for('courses_view'))


@app.context_processor
def inject_user_details():
    user = users.get_current_user()
    if user:
        Student.get_or_create(user_id=user.user_id())
        action = "Logout"
        url = users.create_logout_url('/')
    else:
        action = "Login"
        url = users.create_login_url('/')
    return {'action': action, 'user_url': url}


@app.route("/fuckshit/avenue")
def fuck_my_shit_up():
    create_tables()
    with open("soc.html", "r") as f:
        parse_html(f)

    return "Aye"


if __name__ == "__main__":
    # If the database doesn't exist, create it.
    if not os.path.exists(DB_NAME):
        create_tables()

        with open("soc.html", "r") as f:
            parse_html(f)
