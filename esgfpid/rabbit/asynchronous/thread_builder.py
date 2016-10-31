import logging
import pika
import time
import copy
from esgfpid.utils import get_now_utc_as_formatted_string as get_now_utc_as_formatted_string
import esgfpid.defaults as defaults
import esgfpid.rabbit.connparams
from esgfpid.utils import loginfo, logdebug, logtrace, logerror, logwarn, log_every_x_times

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())

class ConnectionBuilder(object):
    
    def __init__(self, thread, statemachine, confirmer, returnhandler, shutter, args):
        self.thread = thread
        self.statemachine = statemachine

        '''
        We need to pass the "confirmer.on_delivery_confirmation()" callback to
        RabbitMQ's channel.'''
        self.confirmer = confirmer
        
        '''
        We need to pass the "returnhandler.on_message_not_accepted()"" callback
        to RabbitMQ's channel as "on_return_callback" '''
        self.returnhandler = returnhandler

        '''
        (1) ...
        '''
        self.shutter = shutter

        ''' To count how many times we have tried to reconnect to the same RabbitMQ URL.'''
        self.__reconnect_counter = 0


        self.__store_settings_for_rabbit(args)
        self.__inform_about_settings()

    def __store_settings_for_rabbit(self, args):
        self.__args = args # If we need to reset them
        self.__other_rabbitmq_hosts = copy.copy(args['urls_fallback'])
        self.__current_rabbitmq_host = copy.copy(args['url_preferred'])
        self.__rabbitmq_credentials = copy.copy(args['credentials'])

    def __inform_about_settings(self):
        logdebug(LOGGER, 'Messaging server username: "%s".', self.__rabbitmq_credentials.username)
        logdebug(LOGGER, 'Messaging server password: "%s".', self.__rabbitmq_credentials.password)
        logdebug(LOGGER, 'Messaging server URL: "%s".', self.__current_rabbitmq_host)

        if len(self.__other_rabbitmq_hosts) > 0:
            for i in xrange(len(self.__other_rabbitmq_hosts)):
                logdebug(LOGGER, 'Alternative URLs %i: "%s".', i+1, self.__other_rabbitmq_hosts[i])
        else:
            logdebug(LOGGER, 'No alternative URLs provided.')

    ####################
    ### Start ioloop ###
    ####################

    '''
    Entry point. Called once to trigger the whole
    (re) connection process. Called from run method of the rabbit thread.
    '''
    def first_connection(self):
        logdebug(LOGGER, 'Trigger connection to rabbit...')
        self.__trigger_connection_to_rabbit_etc()
        logdebug(LOGGER, 'Trigger connection to rabbit... done.')
        logdebug(LOGGER, 'Start waiting for events...')
        self.__start_waiting_for_events()
        logtrace(LOGGER, 'Had started waiting for events, but stopped.')
    
    def __start_waiting_for_events(self, max_retries=10, retry_seconds=0.5): # TODO Put these values into config!
        '''
        This waits until the whole chain of callback methods triggered by
        "trigger_connection_to_rabbit_etc()" has finished, and then starts 
        waiting for publications.
        This is done by starting the ioloop.

        Note: In the pika usage example, these things are both called inside the run()
        method, so I wonder if this check-and-wait here is necessary. Maybe not.
        But the usage example does not implement a Thread, so it probably blocks during
        the opening of the connection. Here, as it is a different thread, the run()
        might get called before the __init__ has finished? I'd rather stay on the
        safe side, as my experience of threading in Python is limited.
        '''
        counter_of_tries = 0
        while True:
            counter_of_tries += 1         

            # Start ioloop if connection object ready:
            if self.thread._connection is not None:
                try:
                    logdebug(LOGGER, 'Starting ioloop...')
                    logtrace(LOGGER, 'ioloop is owned by connection %s...', self.thread._connection)
                    logdebug(LOGGER, 'Starting ioloop, can now fire events...')

                    # Tell the main thread that we're now open for events.
                    # As soon as the thread._connection object is not None anymore, it
                    # can receive events.
                    # TODO Or do we need to wait for the ioloop to be started? In that case,
                    # the "...stop_waiting..." would have to be called after starting the
                    # ioloop, which does not work, as the ioloop.start() blocks.
                    self.thread.tell_publisher_to_stop_waiting_for_thread_to_accept_events() 
                    self.thread.continue_gently_closing_if_applicable()
                    self.thread._connection.ioloop.start()
                    break

                except pika.exceptions.ProbableAuthenticationError as e:
                    # If the authentication exception is thrown during connection startup,
                    # is it caught here?
                    # TODO Test this!
                    logerror(LOGGER, 'Cannot properly start the thread. Caught Authentication Exception: %s %s', e.__class__.__name__, e.message)
                    self.statemachine.set_to_permanently_unavailable() # to make sure no more messages are accepted, and gentle-finish won't wait...
                    self.statemachine.detail_authentication_exception = True
                    self.thread._connection.ioloop.start() # to be able to listen to finish events from main thread!
                    break

                except Exception as e:
                    # Does this catch any error during connection startup, or even during
                    # the entire time the ioloop runs, blocks and waits for events?
                    logerror(LOGGER, 'Unexpected error when starting to listen to events: %s: %s', e.__class__.__name__, e.message)
                    break

            # Otherwise, wait and retry
            elif counter_of_tries < max_retries:
                logdebug(LOGGER, 'Very unexpected: Connection object is not ready in try %i/%i. Trying again after %i seconds.', counter_of_tries, max_retries, retry_seconds)
                time.sleep(retry_seconds)

            # If we have reached the max number of retries:
            # TODO I don't think that this can happen, as the connection object
            # always exists, no matter if the actual connection to RabbitMQ
            # succeeds of not.
            else:
                logdebug(LOGGER, 'Very unexpected: Connection object is not ready in try %i/%i. Giving up.', counter_of_tries, max_retries)
                logerror(LOGGER, 'Cannot properly start the thread. Connection object is not ready.')
                break

    ########################################
    ### Chain of callback functions that ###
    ### connect to rabbit                ###
    ########################################

    def __trigger_connection_to_rabbit_etc(self):
        self.__please_open_connection()

    ''' Asynchronous, waits for answer from RabbitMQ.'''
    def __please_open_connection(self):
        logdebug(LOGGER, 'Connecting to RabbitMQ at %s... (%s)', self.__current_rabbitmq_host, get_now_utc_as_formatted_string())
        params = esgfpid.rabbit.connparams.get_connection_parameters(
            self.__rabbitmq_credentials,
            self.__current_rabbitmq_host)
        loginfo(LOGGER, 'Opening connection to RabbitMQ...')
        self.thread._connection = pika.SelectConnection(
            parameters=params,
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_error,
            on_close_callback=None,
            stop_ioloop_on_close=False
        )

    ''' Callback, called by RabbitMQ.'''
    def on_connection_open(self, unused_connection):
        logdebug(LOGGER, 'Opening connection... done.')
        loginfo(LOGGER, 'Connection to RabbitMQ at %s opened... (%s)', self.__current_rabbitmq_host, get_now_utc_as_formatted_string())

        # Tell the main thread we're open for events now:
        # When the connection is open, the thread is ready to accept events.
        # Note: It was already ready when the connection object was created,
        # not just now that it's actually open. So this second call to
        # "...stop_waiting..." should be redundant!
        self.thread.tell_publisher_to_stop_waiting_for_thread_to_accept_events()
        self.__add_on_connection_close_callback()
        self.__please_open_rabbit_channel()

    ''' Asynchronous, waits for answer from RabbitMQ.'''
    def __please_open_rabbit_channel(self):
        logdebug(LOGGER, 'Opening channel...')
        self.thread._connection.channel(on_open_callback=self.on_channel_open)

    ''' Callback, called by RabbitMQ. '''
    def on_channel_open(self, channel):
        logdebug(LOGGER, 'Opening channel... done.')
        logtrace(LOGGER, 'Channel has number: %i.', channel.channel_number)
        self.thread._channel = channel
        self.__reconnect_counter = 0
        self.__add_on_channel_close_callback()
        self.__add_on_return_callback()
        self.__make_channel_confirm_delivery()
        self.__make_ready_for_publishing()

    def __make_channel_confirm_delivery(self):
        logtrace(LOGGER, 'Set confirm delivery... (Issue Confirm.Select RPC command)')
        self.thread._channel.confirm_delivery(callback=self.confirmer.on_delivery_confirmation)
        logdebug(LOGGER, 'Set confirm delivery... done.')
 
    def __make_ready_for_publishing(self):
        logdebug(LOGGER, '(Re)connection established, making ready for publication...')

        # Check for unexpected errors:
        if self.thread._channel is None:
            logerror(LOGGER, 'Channel is None after connecting to server. This should not happen.')
            self.statemachine.set_to_permanently_unavailable()
        if self.thread._connection is None:
            logerror(LOGGER, 'Connection is None after connecting to server. This should not happen.')
            self.statemachine.set_to_permanently_unavailable()

        # Normally, it should already be waiting to be available:
        if self.statemachine.is_WAITING_TO_BE_AVAILABLE():
            logdebug(LOGGER, 'Setup is finished. Publishing may start.')
            logtrace(LOGGER, 'Publishing will use channel no. %s!', self.thread._channel.channel_number)
            self.statemachine.set_to_available()
            self.__check_for_already_arrived_messages_and_publish_them()

        # It was asked to close in the meantime (but might be able to publish the last messages):
        elif self.statemachine.is_AVAILABLE_BUT_WANTS_TO_STOP():
            logdebug(LOGGER, 'Setup is finished, but the module was already asked to be closed in the meantime.')
            self.__check_for_already_arrived_messages_and_publish_them()

        # It was force-closed in the meantime:
        elif self.statemachine.is_PERMANENTLY_UNAVAILABLE(): # TODO called by whom?
            if self.statemachine.detail_closed_by_publisher:
                logdebug(LOGGER, 'Setup is finished now, but the module was already force-closed in the meantime.')
                self.shutter.safety_finish('closed before connection was ready. reclosing.') # TODO Needed?
            elif self.statemachine.detail_could_not_connect:
                logerror(LOGGER, 'This is not supposed to happen. If the connection failed, this part of the code should not be reached.')
            else:
                logerror(LOGGER, 'This is not supposed to happen. An unknown event set this module to be unavailable. When was this set to unavailable?')
        else:
            logdebug(LOGGER, 'Unexpected state.')

    def __check_for_already_arrived_messages_and_publish_them(self):
        logdebug(LOGGER, 'Checking if messages have arrived in the meantime...')
        num = self.thread.get_num_unpublished()
        if num > 0:
            loginfo(LOGGER, 'Ready to publish messages to RabbitMQ. %i messages are already waiting to be published.', num)
            for i in xrange(num):
                self.thread.add_event_publish_message()
        else:
            loginfo(LOGGER, 'Ready to publish messages to RabbitMQ.')
        

    ########################
    ### Connection error ###
    ########################

    '''
    If the connection to RabbitMQ failed, there is various
    things that may happen:
    (1) If there is other RabbitMQ urls, it will try to connect 
        to one of these.
    (2) If there is no other URLs, it will try to reconnect to this
        one after a short waiting time.
    (3) If the maximum number of reconnection tries is reached, it
        gives up.
    '''
    def on_connection_error(self, connection, msg):

        # If there is alternative URLs, try one of those:
        if self.__is_fallback_url_left():
            oldhost = self.__current_rabbitmq_host
            logdebug(LOGGER, 'Failed connecting to "%s": %s. %i fallback URLs left to try.', oldhost, msg, len(self.__other_rabbitmq_hosts))
            self.__choose_next_host(msg)
            loginfo(LOGGER, 'Failed connection to RabbitMQ at %s. Reason: %s. Now trying to connect to %s.', oldhost, msg, self.__current_rabbitmq_host)
            reopen_seconds = 0
            self.__wait_and_trigger_reconnection(connection, reopen_seconds)
            # Use "__wait_and_trigger_reconnection()" instead of "__please_open_connection()", because
            # the latter does not stop and restart the ioloop, so the ioloop listens to a different
            # connection object than the new connection!

        # If no alternatives are left, retry connecting to
        # the current one (but only a given number of times)
        else:
            self.__reconnect_counter += 1;
            if self.__reconnect_counter <= defaults.RABBIT_ASYN_RECONNECTION_MAX_TRIES:
                loginfo(LOGGER, 'Failed connecting to RabbitMQ at %s. Will retry (%i/%i).', self.__current_rabbitmq_host, self.__reconnect_counter, defaults.RABBIT_ASYN_RECONNECTION_MAX_TRIES)
                #self.__store_settings_for_rabbit(self.__args) # Resetting hosts... TODO Why are we resetting the host here?
                reopen_seconds = defaults.RABBIT_ASYN_RECONNECTION_SECONDS
                self.__wait_and_trigger_reconnection(connection, reopen_seconds)

            # If we have tried to connect many times, just give up:
            else:
                self.statemachine.set_to_permanently_unavailable()
                self.statemachine.detail_could_not_connect = True
                logdebug(LOGGER, 'Failed connection to "%s": %s. No fallback URL left to try.', self.__current_rabbitmq_host, msg)
                logwarn(LOGGER, 'Permanently failed to connect to RabbitMQ. No PID requests will be sent.')
                return None # TODO Why return None here? Necessary?

    def __is_fallback_url_left(self):
        num_fallbacks = len(self.__other_rabbitmq_hosts)
        if num_fallbacks > 0:
            return True
        else:
            return False

    def __choose_next_host(self, msg):
        nexthost = self.__other_rabbitmq_hosts.pop()
        self.__current_rabbitmq_host = nexthost

    #############################
    ### React to channel and  ###
    ### connection close      ###
    #############################

    ''' This tells RabbitMQ what to do if it receives 
    a message it cannot accept, e.g. if it cannot
    route it. '''
    def __add_on_return_callback(self):
        self.thread._channel.add_on_return_callback(self.returnhandler.on_message_not_accepted)

    ''' This tells RabbitMQ what to do if the connection
    was closed. '''
    def __add_on_connection_close_callback(self):
        self.thread._connection.add_on_close_callback(self.on_connection_closed)

    '''
    This tells RabbitMQ what to do if the channel
    was closed.

    Note: Every connection close includes a channel close.
    However, as far as I know, this callback is only
    called if the channel is closed without the underlying
    connection being closed. I am not 100 percent sure though.
    '''
    def __add_on_channel_close_callback(self):
        self.thread._channel.add_on_close_callback(self.on_channel_closed)

    '''
    Callback, called by RabbitMQ.
    "on_channel_closed" can be called in three situations:

    (1) The user asked to close the connection.
        In this case, we want to clean up everything and leave it closed.

    (2) The connection was closed because we tried to publish to a non-
        existent exchange.
        In this case, the connection is still open, and we want to reopen
        a new channel and publish to a different exchange.
        We also want to republish the ones that had failed.

    (3) There was some problem that closed the connection, which causes
        the channel to close.
        In this case, we want to reopen a connection.
        TODO I THINK IN THIS CASE, RabbitMQ ONLY CALLS ON_CONNECTION_CLOSED,
        NOT ON_CHANNEL_CLOSED!!!
        We trigger reconnection anyway.

    '''
    def on_channel_closed(self, channel, reply_code, reply_text):
        logdebug(LOGGER, 'Channel was closed: %s (code %i)', reply_text, reply_code)

        # Channel closed because user wants to close:
        if self.statemachine.is_PERMANENTLY_UNAVAILABLE():
            if self.statemachine.detail_closed_by_publisher:
                logdebug(LOGGER,'Channel close event due to close command by user. This is expected.')

        # Channel closed because exchange did not exist:
        elif reply_code == 404:
            logdebug(LOGGER, 'Channel closed because the exchange "%s" did not exist.', self.thread.get_exchange_name())
            self.__use_different_exchange_and_reopen_channel()

        # Other unexpected channel close:
        else:
            logerror(LOGGER,'Unexpected channel shutdown. Need to close connection to trigger all the necessary close down steps.')
            # TODO Is this called every time a connection is closed?
            self.thread._connection.close()

    '''
    An attempt to publish to a nonexistent exchange will close
    the channel. In this case, we use a different exchange name
    and reopen the channel. The underlying connection was kept
    open.
    '''
    def __use_different_exchange_and_reopen_channel(self):

        # Set to waiting to be available, so that incoming
        # messages are stored:
        self.statemachine.set_to_waiting_to_be_available()

        # New exchange name
        logdebug(LOGGER, 'Setting exchange name to fallback exchange "%s"', defaults.RABBIT_FALLBACK_EXCHANGE_NAME)
        self.thread.set_exchange_name(defaults.RABBIT_FALLBACK_EXCHANGE_NAME)

        # If this happened while sending message to the wrong exchange, we
        # have to trigger their resending...
        self.__prepare_channel_reopen('Channel reopen')

        # Reopen channel
        # TODO Reihenfolge richtigen? Erst prepare, dann open?
        logdebug(LOGGER, 'Reopening channel...')
        self.statemachine.set_to_waiting_to_be_available()
        self.__please_open_rabbit_channel()

    '''
    Callback, called by RabbitMQ.
    "on_connection_closed" can be called in two situations:

    (1) The user asked to close the connection.
        In this case, we want to clean up everything and leave it closed.

    (2) There was some other problem that closed the connection.

    '''
    def on_connection_closed(self, connection, reply_code, reply_text):
        loginfo(LOGGER, 'Connection to RabbitMQ was closed. Reason: %s.', reply_text)
        self.thread._channel = None
        if self.__was_user_shutdown(reply_code, reply_text):
            loginfo(LOGGER, 'Connection to %s closed.', self.__current_rabbitmq_host)
            self.make_permanently_closed_by_user()
        else:
            reopen_seconds = defaults.RABBIT_ASYN_RECONNECTION_SECONDS
            self.__wait_and_trigger_reconnection(connection, reopen_seconds)

    def __was_user_shutdown(self, reply_code, reply_text):
        if self.__was_forced_user_shutdown(reply_code, reply_text):
            return True
        elif self.__was_gentle_user_shutdown(reply_code, reply_text):
            return True
        return False

    def __was_forced_user_shutdown(self, reply_code, reply_text):
        if (reply_code==self.thread.ERROR_CODE_CONNECTION_CLOSED_BY_USER and
            self.thread.ERROR_TEXT_CONNECTION_FORCE_CLOSED in reply_text):
            return True
        return False

    def __was_gentle_user_shutdown(self, reply_code, reply_text):
        if (reply_code==self.thread.ERROR_CODE_CONNECTION_CLOSED_BY_USER and
            self.thread.ERROR_TEXT_CONNECTION_NORMAL_SHUTDOWN in reply_text):
            return True
        return False

    ''' Called by thread, by shutter module.'''
    def make_permanently_closed_by_user(self):
        # This changes the state of the state machine!
        # This needs to be called from the shutter module
        # in case there is a force_finish while the connection
        # is already closed (as the callback on_connection_closed
        # is not called then).
        self.statemachine.set_to_permanently_unavailable()
        logtrace(LOGGER, 'Stop waiting for events due to user interrupt!')
        logtrace(LOGGER, 'Permanent close: Stopping ioloop of connection %s...', self.thread._connection)
        self.thread._connection.ioloop.stop()
        loginfo(LOGGER, 'Stopped listening for RabbitMQ events (%s).', get_now_utc_as_formatted_string())
        logdebug(LOGGER, 'Connection to messaging service closed by user. Will not reopen.')

    '''
    This triggers a reconnection to whatever host is stored in
    self.__current_rabbitmq_host at the moment of reconnection.

    If it is called to reconnect to the same host, it is better
    to wait some seconds.

    If it is used to connect to the next host, there is no point
    in waiting.
    '''
    def __wait_and_trigger_reconnection(self, connection, wait_seconds):
        self.statemachine.set_to_waiting_to_be_available()
        loginfo(LOGGER, 'Trying to reconnect to RabbitMQ in %i seconds.', wait_seconds)
        connection.add_timeout(wait_seconds, self.reconnect)
        logtrace(LOGGER, 'Reconnect event added to connection %s (not to %s)', connection, self.thread._connection)

    ###########################
    ### Reconnect after     ###
    ### unexpected shutdown ###
    ###########################

    '''
    Reconnecting creates a completely new connection.
    If we reconnect, we need to reset message number,
    delivery tag etc.

    We need to prepare to republish the yet-unconfirmed
    messages.

    Then we need to stop the old connection's ioloop.
    The reconnection will create a new connection object
    and this will have its own ioloop.

    '''
    def reconnect(self):
        logdebug(LOGGER, 'Reconnecting...')

        # We need to reset delivery tags, unconfirmed messages,
        # republish the unconfirmed, ...
        self.__prepare_channel_reopen('Reconnect')
        
        # This is the old connection ioloop instance, stop its ioloop
        logdebug(LOGGER, 'Reconnect: Stopping ioloop of connection %s...', self.thread._connection)
        self.thread._connection.ioloop.stop()
        # Note: All events still waiting on the ioloop are lost.
        # Messages are kept track of in the Queue.Queue or in the confirmer
        # module. Closing events are kept track on in shutter module.

        # Now we trigger the actual reconnection, which
        # works just like the first connection to RabbitMQ.
        self.first_connection()

    '''
    This is called during reconnection and during channel reopen.
    Both implies that a new channel is opened.
    '''
    def __prepare_channel_reopen(self, operation_string):
        # We need to reset the message number, as
        # it works by channel:
        logdebug(LOGGER, operation_string+': Resetting delivery number (for publishing messages).')
        self.thread.reset_delivery_number()

        # Furthermore, as we'd like to re-publish messages
        # that had not been confirmed yet, we remove them
        # from the stack of unconfirmed messages, and put them
        # back to the stack of unpublished messages.
        logdebug(LOGGER, operation_string+': Sending all messages that have not been confirmed yet...')
        self.__prepare_republication_of_unconfirmed()

        # Reset the unconfirmed delivery tags, as they also work by channel:
        logdebug(LOGGER, operation_string+': Resetting delivery tags (for confirming messages).')
        self.thread.reset_unconfirmed_messages_and_delivery_tags()
        
    def __prepare_republication_of_unconfirmed(self):
        # Get all unconfirmed messages - we won't be able to receive their confirms anymore:
        # IMPORTANT: This has to happen before we reset the delivery_tags of the confirmer
        # module, as this deletes the collection of unconfirmed messages.
        rescued_messages = self.thread.get_unconfirmed_messages_as_list_copy_during_lifetime()
        if len(rescued_messages)>0:
            logdebug(LOGGER, '%i unconfirmed messages were saved and are sent now.', len(rescued_messages))
            self.thread.send_many_messages(rescued_messages)
            # Note: The actual publish of these messages to rabbit
            # happens when the connection is there again, so no wrong delivery
            # tags etc. are created by this line!

