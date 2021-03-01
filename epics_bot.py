#! /usr/bin/python3

from telegram.ext import Updater, CommandHandler
import telegram
from smlib import fsmBase, loader
from bs4 import BeautifulSoup
import time
import json
import argparse

#Private token generated with @BotFather on telegram
signs = {0:'\U0001F7E2', 1:'\U0001F7E0', 2:'\U0001F534', -1:'\U0001F7E0', -2:'\U0001F534', None:'\U0001F534' }


class BotHelper(object):
    def __init__(self, bot, bot_name, authorized_ids, all_subscribers_ids, commands):
        self.bot = bot
        self.bot_name = bot_name
        self.all_subscribers_ids = all_subscribers_ids
        self.authorized_ids = authorized_ids
        self.commands = [command[0] for command in commands]
    
    # Callback on the /start command
    def start(self, update, context):
        kb = [[telegram.KeyboardButton(command)] for command in self.commands]
        context.bot.send_message(chat_id=update.effective_chat.id, 
                                text=f"{self.bot_name}\nAccess restricted to selected personnel.")

    def helper(self, update, context):
        msg = f"{self.bot_name}\n"
        if update.effective_chat.id in self.authorized_ids:
            # Authorized
            msg += "Try:\n"
            for command in self.commands:
                msg += '/' + command.replace("_", "\_") +'\n'
            msg+= "to get and overview of the PVs."
        else:
            msg += f"You are not authorized to access this information. ID={update.effective_chat.id}"
        context.bot.send_message(chat_id=update.effective_chat.id, 
                                text=msg,
                                parse_mode=telegram.ParseMode.MARKDOWN)
                
    def send_all_subscribers(self, msg):
        for user_id in self.all_subscribers_ids:
            self.bot.send_message(chat_id=user_id, text=msg, parse_mode=telegram.ParseMode.MARKDOWN)

# FSM to send messages about groups of PVs
# Such as a report of all values
# Or a notification when all the PVs are connected or disconnected
class SectionNotifier(fsmBase):
    def __init__(self, name, section, bot, pvs, subscribers_ids, authorized_ids, antibounce, *args, **kwargs):
        super(SectionNotifier, self).__init__(name, **kwargs)
        self.bot = bot
        self.users_conf = users_conf
        self.subscribers_ids = subscribers_ids
        self.authorized_ids = authorized_ids
        self.pvs = {self.connect(pv_name):desc for pv_name, desc in pvs.items()}
        self.section = section
        self.antibounce = antibounce
        
        self.gotoState('offline')
    
    def going_online_entry(self):
        self.tmrSet("online", self.antibounce)

    def going_online_eval(self):
        if not self.allof(self.pvs.keys(), "initialized"):
            self.gotoState("offline")
        elif self.tmrExpired("online"):
            self.logI("Now connected to all PVs")
            msg = f"{signs[0]} *ONLINE*:\n{self.section.upper()} - Connected to all the PVs and ready to send alarm notifications!"
            self.send_to_subscribers(msg)
            self.gotoState("online")

    # All PVs are ONLINE
    # notify subscribed users when a PV changes its alarm status
    def online_eval(self):
        if not self.allof(self.pvs.keys(), "initialized"):
            self.gotoState("going_offline")
    
    def going_offline_entry(self):
        self.tmrSet("offline", self.antibounce)

    def going_offline_eval(self):
        if self.allof(self.pvs.keys(), "initialized"):
            self.gotoState("online")
        elif self.tmrExpired("offline"):
            self.logI("Some PVs are now offline")
            msg = f"{signs[2]} *OFFLINE*:\n{self.section.upper()} - Cannot connect to the PVs, notifications are not active!"
            self.send_to_subscribers(msg)
            self.gotoState("offline")

    # At least one PV is OFFLINE
    def offline_eval(self):
        if self.allof(self.pvs.keys(), "initialized"):
            self.gotoState("going_online")


    # Send a report with all the PV when asked
    # For each pv print PV: value
    def status(self):
        msg = f'*{self.section.upper()}*\n'
        for pv, desc in self.pvs.items():
            if pv.initialized():
                msg += f"{signs.get(pv.alarm(), '')} {desc}: {pv.val():.2e}\n"
            else:
                msg += f"{signs[2]} {desc}: OFFLINE\n"
        return msg

    # Callback when a user send a command to request the status of this section
    def answer_request(self, update, context):
        user_id = update.effective_chat.id
        msg = 'Unknown Error'
        if user_id not in authorized_ids:
            self.logE(f"Unauthorized access to {self.section}")
            msg = f"You are not authorized to access this information. ID={user_id}"
        else:
            msg = self.status()

        context.bot.send_message(chat_id=user_id,
                                 text=msg,
                                 parse_mode=telegram.ParseMode.MARKDOWN)

    # Utility to send a message to all the subscribers to this section
    def send_to_subscribers(self, msg):
        for user_id in self.subscribers_ids:
            self.logI(f"Sending message to {user_id}")
            self.bot.send_message(chat_id=user_id, text=msg, parse_mode=telegram.ParseMode.MARKDOWN)
            time.sleep(0.1)

# FSM to send messages about one single PV
# When it changes value
# It's done on a dedicated FSM to use the timers and antibounce the alarm
class PVNotifier(fsmBase):
    def __init__(self, name, pv, desc, sectionFSM, antibounce, *args, **kwargs):
        super(PVNotifier, self).__init__(name, **kwargs)
        self.pv = self.connect(pv)
        self.desc = desc
        self.section = sectionFSM.section
        self.sectionFSM = sectionFSM
        self.antibounce = antibounce
        
        self.gotoState('ready')

    # When the alarm changes
    def ready_eval(self):
        if self.pv.initialized() and self.pv.alarmChanging():
            self.prev_alarm = self.pv._psevr
            self.gotoState("wait")

    # Wait for 30s
    def wait_entry(self):
        self.tmrSet("antibounce", self.antibounce)

    # If the alarm went back to the previous level, do nothing
    # else, send current alarm level
    def wait_eval(self):
        if self.tmrExpired("antibounce"):
            if self.pv.initialized() and self.pv.alarm() != self.prev_alarm:
                msg = f"{signs.get(self.pv.alarm(), '')} *{self.pv.alarmName(short=True)}*:\n{self.section.upper()} - {self.desc}\nValue: {self.pv.val():.2e}"
                self.sectionFSM.send_to_subscribers(msg)
            self.gotoState("ready")


# Main
if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Telegram EPICS Alarms bot')
    parser.add_argument('config', type=str, help='Configuration json file')
    args = parser.parse_args()
    print(args.config)
    available_commands = [('help', 'Display help')]

    # Load users configuration
    with open(args.config, 'r') as f:
        users_conf = json.load(f)
    antibounce = users_conf.get('antibounce', 30)
    bot_token = users_conf['bot_token']
    bot_name = users_conf.get('bot_name', "Control System Bot!")
    sections = users_conf['subscriptions'].keys()
    authorized_users = users_conf['users']
    authorized_ids = authorized_users.values()
    all_subscribers_names = set([v for sv in users_conf['subscriptions'].values() for v in sv])
    all_subscribers_ids = set([authorized_users[name] for name in all_subscribers_names])

    # Define the Updater and Dispatcher used to handle bot actions
    updater = Updater(token=bot_token, use_context=True)
    dispatcher = updater.dispatcher
    bot = updater.bot

    l = loader()

    for sect in sections:
        print(sect)
        # Add command for this section
        available_commands.append((f'{sect}_status', f'Request an overview of {sect} alarms'))
        
        # Load PVs
        with open(f'./config/{sect}.xml') as f:
            soup = BeautifulSoup(f, 'xml')
        pv_components = soup.find_all('pv')
        
        # Extract PV names and descriptions
        pvs = {}
        for pv_component in pv_components:
            desc = pv_component.description.text if pv_component.description else ""
            desc = desc.replace('*', '')
            pvs[pv_component['name']] = desc

        # load the fsm for this section
        subscribers_names = users_conf['subscriptions'][sect]
        print(f" - Subscribers names: {subscribers_names}")
        subscribers_ids = [authorized_users[name] for name in subscribers_names]
        print(f" - Subscribers IDs: {subscribers_ids}")
        fsm = l.load(SectionNotifier, f'{sect}_notifier', sect.replace("_", " "), bot, pvs, subscribers_ids, authorized_ids, antibounce)

        for pv, desc in pvs.items():
            l.load(PVNotifier, pv, pv, desc, fsm, antibounce)

        # Register callback handler
        handler = CommandHandler(f'{sect}_status', fsm.answer_request)
        dispatcher.add_handler(handler)

    # Set command help
    bot_helper = BotHelper(bot, bot_name, authorized_ids, all_subscribers_ids, available_commands)
    bot_helper.send_all_subscribers("Bot powered ON")
    bot.set_my_commands(available_commands)

    # Link callbacks to commands
    start_handler = CommandHandler('start', bot_helper.start)
    dispatcher.add_handler(start_handler)

    help_handler = CommandHandler('help', bot_helper.helper)
    dispatcher.add_handler(help_handler)

    # Start the bot
    updater.start_polling()
    #updater.idle()

    # start fsm execution
    l.start()
    bot_helper.send_all_subscribers("Bot powered OFF")
    updater.stop()
