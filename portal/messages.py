import emoji

from portal.utils import fmt


def build_warning_parent_message(name, warning_number, max_warnings, now_suspended=False):
    """Warning sent to the parent when the student has not submitted a homework or quiz on time."""
    ordinals = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth"}
    label = f"{ordinals.get(int(warning_number), f'#{warning_number}')} Warning ({warning_number}/{max_warnings})"

    lines = [
        "🌟 Dear Parent,",
        "",
        "تحية طيبة لحضراتكم ❤️",
        "",
        "حرصًا منا على متابعة المستوى الأكاديمي والانضباط الدراسي لأبنائنا الطلاب، نود إحاطة سيادتكم علمًا بأن الطالب:",
        "",
        f"👨‍🎓{name}",
        f"قد حصل على ⚠️ {label}، وذلك نتيجة عدم الالتزام بتسليم الواجب أو أداء الامتحان في الموعد المحدد دون تقديم عذر مقبول.",
        "",
    ]
    if now_suspended:
        # Only on the warning that reaches the limit: the account is suspended as of now.
        lines += [
            "⛔ تم إيقاف حساب الطالب، يرجى التواصل مع المدرس أو المساعدين لحل المشكلة.",
            "",
        ]
    lines += [
        "📌 نرجو من سيادتكم:",
        "متابعة الطالب والتأكيد على ضرورة الالتزام بمواعيد تسليم الواجبات والامتحانات، حيث إن الوصول إلى الإنذار الثالث قد يترتب عليه إيقاف الحساب بشكل كامل وفقًا لسياسة المنصة.",
        "",
        "🤝 نؤمن أن تعاون الأسرة مع فريق العمل هو أحد أهم عوامل نجاح الطالب وتحقيق أفضل النتائج، ونقدّر لكم دعمكم المستمر وحرصكم على مصلحة أبنائنا.",
        "",
        "💙 With Best Regards,",
        "Team Mr. Mahmoud Adel",
        "",
        "━━━━━━━━━━━━━━",
        "🤖 Automated Notification: This message was generated automatically. Occasional errors may occur.",
        "هذه الرسالة التلقائية والأخطاء قد تحدث اذا شككت في وجود مشكلة تابع مع الدعم",
    ]
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
