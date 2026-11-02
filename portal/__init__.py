"""Grade portal UI and logic, split out of app.py (which is now only the entry point and router).

    config.py      constants, KINDS table, Drive script URL
    utils.py       small value/format helpers
    images.py      photo stitching, thumbnails, uploads -> PDF
    messages.py    WhatsApp message text for grades / missing work
    locks.py       in-memory "being graded" locks shared by admin sessions
    data.py        database handle, cached reads, run_write() + cache invalidation
    grades.py      saving grades and homework submissions
    reports.py     cached PDF report builders
    views/         one module per screen: public (login + homework submission), parent,
                   admin (navigation), grading, homeworks, quizzes, students, broadcast
"""
