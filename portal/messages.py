import emoji

from portal.utils import fmt


def build_warning_parent_message(name, warning_number, max_warnings, now_suspended=False):
    """Warning sent to the parent when the student has not submitted a homework or quiz on time."""
    msg = (
        f"اهلا بك يا ولي الامر الكريم لقد حصل ابنك الطالب {name} على انذار {warning_number} من اصل {max_warnings} "
        "لعدم تسليمة الواجب او الامتحان في المعاد المناسب بدون عذر يرجى متابعة الطالب حتى لا يتم ايقاف حسابة "
        "بالكامل ونشكركم شكرا جزيلا على تعاونكم "
    )
    lines = [msg]
    if now_suspended:
        lines.append("تم ايقاف حساب الطالب، يرجى التواصل مع المدرس او المساعدين لحل المشكلة")
    lines.append("(ملحوظة:هذه الرسالة تلقائية والاخطاء قد تحدث)")
    lines.append("team Mr. Mahmoud Adel")
    return chr(10).join(lines)


def build_wa_messages(kind, name, title, score, total):
    """Returns (parent_message, student_message)."""
    if score is not None:
        type_ar = "Quiz" if kind == "quiz" else "Homework"
        parent = emoji.emojize(
            f":bar_chart: درجة الـ {title}\n\n"
            f"ولي الأمر الكريم،\n"
            f"نحيط حضرتكم علمًا بأن الطالب {name} حصل على {fmt(score)} / {fmt(total)} في الـ {type_ar} الأخير.\n\n"
            f"نتمنى له مزيدًا من التقدم والنجاح، ونسعى دائمًا لمتابعة مستوى الطالب بشكل مستمر وتحسين نقاط الضعف أولًا بأول. :glowing_star:\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
        student = emoji.emojize(
            f":bar_chart: درجة الـ {title}\n\n"
            f"أهلاً بك يا {name}،\n"
            f"لقد حصلت على {fmt(score)} / {fmt(total)} في الـ {type_ar} الأخير.\n\n"
            f"استمر في المذاكرة والتدريب، ونتمنى لك دوام التفوق والنجاح! :glowing_star:\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
    elif kind == "homework":
        parent = emoji.emojize(
            f":bar_chart: homework\n\n"
            f"نحيط حضرتكم علمًا بأن\n"
            f"الطالب: {name}\n"
            f"لم يقم بأداء واجب  : {title}\n\n"
            f"برجاء الالتزام بحضور وأداء الواجبات في المواعيد المحددة، ومتابعة جميع التقييمات أولًا بأول.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
        student = emoji.emojize(
            f":bell: تذكير بـ homework\n\n"
            f"أهلاً بك يا {name}،\n"
            f"نذكرك بأنه لم يتم تسجيل أداءك في واجب : {title}\n\n"
            f"برجاء سرعة إتمام الواجب والالتزام بالمواعيد المحددة.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
    else:
        parent = emoji.emojize(
            f":bar_chart:  quiz\n\n"
            f"نحيط حضرتكم علمًا بأن\n"
            f"الطالب: {name}\n"
            f"لم يقم بأداء امتحان : {title}\n\n"
            f"برجاء الالتزام بحضور وأداء الاختبارات في المواعيد المحددة، ومتابعة جميع التقييمات أولًا بأول.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
        student = emoji.emojize(
            f":bell: تذكير بـ quiz\n\n"
            f"أهلاً بك يا {name}،\n"
            f"نذكرك بأنه لم يتم تسجيل أداءك في كويز : {title}\n\n"
            f"برجاء سرعة إتمام الاختبار والالتزام بالمواعيد المحددة.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
    return parent, student
