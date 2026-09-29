"""Deterministic replies — sent with NO main-model call (02 §1 routing table).

Wording is HMH Labz's, not the spec's (the spec names these replies but does not write them).
Tenant specifics come only from {agent_name} / {business_name}. None of these contain a number.
"""

from __future__ import annotations

from typing import Final, Literal

Kind = Literal[
    "optout_confirmed", "complaint_ack", "greeting", "smalltalk_ack", "holding", "handover"
]
Language = Literal["en", "ar", "ar-latn"]

DISCLOSURE: Final[dict[str, str]] = {
    "en": "Hello! I'm {agent_name}, {business_name}'s automated assistant — our team is here too if you need them.",
    "ar": "مرحباً! أنا {agent_name}، المساعد الآلي لدى {business_name} — وفريقنا موجود أيضاً إذا احتجت إليهم.",
    "ar-latn": "Marhaba! Ana {agent_name}, el assistant el automatic la {business_name} — w el team mawjoud iza bt7tajhom.",
}

_TEXT: Final[dict[str, dict[str, str]]] = {
    "optout_confirmed": {
        "en": "Done — you won't receive offers or promotional messages from {business_name} any more. You can still message us here any time for orders or help.",
        "ar": "تم — لن تصلك بعد الآن عروض أو رسائل ترويجية من {business_name}. ويمكنك مراسلتنا هنا في أي وقت للطلبات أو المساعدة.",
        "ar-latn": "Tamam — ma 7a yewsalak offers aw promotional messages men {business_name} ba3d el yom. Fik tebe3atelna hon ay wa2t lal orders aw el mosa3ade.",
    },
    "complaint_ack": {
        "en": "I'm sorry about this. I've passed it to the {business_name} team and a person will get back to you here shortly.",
        "ar": "نعتذر عن ذلك. لقد أحلت الأمر إلى فريق {business_name} وسيتواصل معك أحد الموظفين هنا قريباً.",
        "ar-latn": "Mnet2assaf 3ala hal shi. 7awwalt el mawdou3 la team {business_name} w shakhs 7a yrodd 3alek hon 2arib.",
    },
    "greeting": {
        "en": "How can I help you today?",
        "ar": "كيف يمكنني مساعدتك اليوم؟",
        "ar-latn": "Kif fini se3dak el yom?",
    },
    "smalltalk_ack": {
        "en": "You're welcome! Message me any time you need anything.",
        "ar": "على الرحب والسعة! راسلني في أي وقت تحتاج فيه إلى شيء.",
        "ar-latn": "3afwan! Bas ba3atli ay wa2t bt7taj shi.",
    },
    "holding": {
        "en": "Let me check that with the team — someone will confirm here shortly.",
        "ar": "دعني أتحقق من ذلك مع الفريق — وسيؤكد لك أحدهم هنا قريباً.",
        "ar-latn": "Khalini et2akkad ma3 el team — 7ada 7a y2akkedlak hon 2arib.",
    },
    "handover": {
        "en": "I've passed this to the {business_name} team — a person will reply here shortly.",
        "ar": "لقد أحلت هذا إلى فريق {business_name} — وسيرد عليك أحد الموظفين هنا قريباً.",
        "ar-latn": "7awwalt hal shi la team {business_name} — shakhs 7a yrodd 3alek hon 2arib.",
    },
}


def canned(
    kind: Kind,
    language: str | None,
    *,
    agent_name: str,
    business_name: str,
    with_disclosure: bool = False,
) -> str:
    lang = language if language in ("en", "ar", "ar-latn") else "en"
    body = _TEXT[kind][lang]
    if with_disclosure:
        body = DISCLOSURE[lang] + " " + body
    return body.format(agent_name=agent_name, business_name=business_name)
