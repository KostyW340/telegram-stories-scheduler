from aiogram.fsm.state import State, StatesGroup


class CreateStoryStates(StatesGroup):
    choosing_type = State()
    choosing_days = State()
    waiting_media = State()
    waiting_caption = State()
    waiting_date = State()
    waiting_date_manual = State()
    waiting_time_choice = State()
    waiting_time_input = State()


class DeleteStoryStates(StatesGroup):
    waiting_job_id = State()


class ManualSendStoryStates(StatesGroup):
    waiting_job_id = State()
