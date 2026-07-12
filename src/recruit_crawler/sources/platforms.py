from __future__ import annotations

from typing import Final

from .platform_jobkorea import JobKoreaAdapter
from .platform_jumpit import JumpitAdapter
from .platform_linkedin import LinkedInAdapter
from .platform_rallit import RallitAdapter
from .platform_rocketpunch import RocketPunchBrowserAutomationAdapter
from .platform_saramin import SaraminAdapter
from .platform_shared import CompanyCareersAdapter
from .platform_wanted import WantedAdapter

PLATFORM_ADAPTERS: Final = {
    "company_careers": CompanyCareersAdapter,
    "jumpit": JumpitAdapter,
    "rallit": RallitAdapter,
    "saramin": SaraminAdapter,
    "jobkorea": JobKoreaAdapter,
    "wanted": WantedAdapter,
    "rocketpunch": RocketPunchBrowserAutomationAdapter,
    "linkedin": LinkedInAdapter,
}


def known_platform_ids() -> list[str]:
    return sorted(PLATFORM_ADAPTERS)
