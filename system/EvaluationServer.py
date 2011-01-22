#!/usr/bin/python
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2010-2011 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2011 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2011 Matteo Boscariol <boscarim@hotmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import threading
import heapq
import time
import xmlrpclib
import signal
from RPCServer import RPCServer

import Configuration
import Utils
import CouchObject
from View import RankingView

class JobQueue:
    """A job is a couple (job_type, submission).

    Elements of the queue are of the form (priority, timestamp, job).
    """

    def __init__(self):
        self.main_lock = threading.RLock()
        self.queue = []
        # The semaphore counts the number of items in the queue
        self.semaphore = threading.Semaphore(0)

    def lock(self):
        self.main_lock.acquire()

    def unlock(self):
        self.main_lock.release()

    def push(self, job, priority, timestamp = None):
        if timestamp == None:
            try:
                timestamp = job[1].timestamp
            except:
                timestamp = time.time()
        with self.main_lock:
            heapq.heappush(self.queue, (priority, timestamp, job))
        self.semaphore.release()

    def top(self):
        with self.main_lock:
            if len(self.queue) > 0:
                return self.queue[0]
            else:
                raise LookupError("Empty queue")

    def pop(self):
        self.semaphore.acquire()
        with self.main_lock:
            try:
                return heapq.heappop(self.queue)
            except IndexError:
                Utils.log("Job queue went out-of-sync with semaphore", Utils.Logger.SEVERITY_CRITICAL)
                raise RuntimeError("Job queue went out-of-sync with semaphore")

    def search(self, job):
        with self.main_lock:
            for i in self.queue:
                # FIXME - Is this `for' correct?
                if self.queue[i][2] == job:
                    return i
        raise LookupError("Job not present in queue")

    # FIXME - Add semaphore handling
    #def delete_by_position(self, pos):
    #    with self.main_lock:
    #        self.queue[pos] = self.queue[-1]
    #        self.queue = self.queue[:-1]
    #        heapq.heapify(self.queue)

    # FIXME - Add semaphore handling
    #def delete_by_job(self, job):
    #    with self.main_lock:
    #        pos = self.search(job)
    #        self.delete_by_position(pos)

    def set_priority(self, job, priority):
        """Change the priority of a job inside the queue.

        Used (only?) when the user use a token, to increase the
        priority of the evaluation of its submission.
        """
        with self.main_lock:
            pos = self.search(job)
            self.queue[pos][0] == priority
            heapq.heapify(self.queue)

    def length(self):
        return len(self.queue)

    def empty(self):
        return self.length() == 0

class WorkerPool:
    WORKER_INACTIVE, WORKER_DISABLED = (None, "disabled")

    def __init__(self):
        self.workers = {}
        # The semaphore counts the number of _inactive_ workers
        self.semaphore = threading.Semaphore(0)
        self.main_lock = threading.RLock()
        self.start_time = {}
        self.address = {}

        # FIXME - Unimplemented
        #self.error_count = [0] * worker_num
        #self.schedule_disabling = [False] * worker_num
        #self.ignore_result = [False] * worker_num

    def acquire_worker(self, job, blocking = True):
        available = self.semaphore.acquire(blocking)
        if not available:
            return None
        with self.main_lock:
            try:
                worker = self.find_worker(self.WORKER_INACTIVE)
            except LookupError:
                Utils.log("Worker array went out-of-sync with semaphore", Utils.Logger.SEVERITY_CRITICAL)
                raise RuntimeError("Worker array went out-of-sync with semaphore")
            self.workers[worker] = job
            return worker

    def release_worker(self, n):
        with self.main_lock:
            if self.workers[n] == self.WORKER_INACTIVE:
                Utils.log("Trying to release worker while it's inactive", Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to release worker while it's inactive")
            self.workers[n] = self.WORKER_INACTIVE
        self.semaphore.release()

    def find_worker(self, job):
        with self.main_lock:
            for worker, workerJob in self.workers.iteritems():
                if workerJob == job:
                    return worker
            raise LookupError("No such job")

    def disable_worker(self, n):
        self.semaphore.acquire()
        with self.main_lock:
            if self.workers[n] != self.WORKER_INACTIVE:
                self.semaphore.release()
                Utils.log("Trying to disable worker while it isn't inactive", Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to release worker while it isn't inactive")
            self.workers[n] = self.WOKER_DISABLED

    def enable_worker(self, n):
        with self.main_lock:
            if self.workers[n] != self.WORKER_DISABLED:
                Utils.log("Trying to enable worker while is isn't disabled", Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to enable worker while is isn't disabled")
            self.workers[n] = self.WORKER_INACTIVE
        self.semaphore.release()

    def add_worker(self, n, host, port):
        with self.main_lock:
            if n in self.workers:
                Utils.log("There is already a worker with name `%s'" % (str(n)))
                raise ValueError("There is already a worker with name `%s'" % (str(n)))
            self.workers[n] = self.WORKER_INACTIVE
            self.start_time[n] = None
            self.address[n] = (host, port)
        self.semaphore.release()

    def del_worker(self, n):
        self.semaphore.acquire()
        with self.main_lock:
            if n not in self.workers:
                self.semaphore.release()
                Utils.log("No worker known with name `%s'" % (str(n)), Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("No worker known with name `%s'" % (str(n)))
            if self.workers[n] != self.WORKER_DISABLED:
                self.sempahore.release()
                Utils.log("Trying to delete a worker while it's not disabled", Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to delete a worker while it's not disabled")
            del self.workers[n]
            del self.start_time[n]
            del self.address[n]

    def working_workers(self):
        with self.main_lock:
            return len(filter(lambda x:
                                  x != self.WORKER_INACTIVE and \
                                  x != self.WORKER_DISABLED,
                              self.workers.values()))

class JobDispatcher(threading.Thread):
    def __init__(self, worker_num):
        threading.Thread.__init__(self)
        self.queue = JobQueue()
        self.workers = WorkerPool()
        for i, w in enumerate(Configuration.workers):
            self.workers.add_worker(i, w[0], w[1])
        self.main_lock = threading.RLock()
        self.bomb_primed = False
        self.touched = threading.Event()

    def check_action(self, priority, timestamp, job):
        """Try to execute the specified action immediately: if it's
        possible, do it and returns True; otherwise returns False."""

        action = job[0]

        # A bomb is never accepted and block the queue forever, so no
        # further jobs can be assigned to workers; moreover, schedule
        # another evaluation of the queue, in order to make the bomb
        # explode if workers are already free.
        if action == EvaluationServer.JOB_TYPE_BOMB:
            if not self.bomb_primed:
                Utils.log("Priming the bomb")
                self.bomb_primed = True
                self.touched.set()
            return False

        else:
            worker = self.workers.acquire_worker(job, blocking = False)

            if worker == None:
                return False

            else:
                submission = job[1]
                self.workers.start_time[worker] = time.time()
                queue_time = self.workers.start_time[worker] - timestamp
                host, port = self.workers.address[worker]

                Utils.log("Asking worker %d (%s:%d) to %s submission %s (after around %.2f seconds of queue)" %
                          (worker,
                           host,
                           port,
                           action,
                           submission.couch_id,
                           queue_time))
                p = xmlrpclib.ServerProxy("http://%s:%d" % (host, port))
                if action == EvaluationServer.JOB_TYPE_COMPILATION:
                    p.compile(submission.couch_id)
                elif action == EvaluationServer.JOB_TYPE_EVALUATION:
                    p.evaluate(submission.couch_id)

                return True

    def process_queue(self):
        with self.main_lock:
            while True:
                try:
                    priority, timestamp, job = self.queue.top()
                except LookupError:
                    # The queue is empty, there is nothing other to do. Good! :-)
                    return

                if self.check_action(priority, timestamp, job):
                    self.queue.pop()
                else:
                    return

    def run(self):
        while True:
            self.touched.wait()
            self.touched.clear()
            with self.main_lock:
                if self.bomb_primed and self.workers.working_workers() == 0:
                    Utils.log("KABOOM!!")
                    return
                self.process_queue()

    def queue_push(self, job, priority, timestamp = None):
        with self.main_lock:
            self.queue.push(job, priority, timestamp)
        self.touched.set()

    def queue_set_priority(self, job, priority):
        with self.main_lock:
            self.queue.set_priority(job, priority)
        self.touched.set()

    def release_worker(self, worker):
        with self.main_lock:
            self.workers.release_worker(worker)
        self.touched.set()

    def find_worker(self, job):
        with self.main_lock:
            return self.workers.find_worker(job)

class EvaluationServer(RPCServer):
    JOB_PRIORITY_EXTRA_HIGH, JOB_PRIORITY_HIGH, JOB_PRIORITY_MEDIUM, JOB_PRIORITY_LOW, JOB_PRIORITY_EXTRA_LOW = range(5)
    JOB_TYPE_COMPILATION, JOB_TYPE_EVALUATION, JOB_TYPE_BOMB = ["compile", "evaluate", "bomb"]
    MAX_COMPILATION_TENTATIVES, MAX_EVALUATION_TENTATIVES = [3, 3]

    def __init__(self, contest, listen_address = None, listen_port = None):
        Utils.log("Evaluation Server for contest %s started..." %
                  (contest.couch_id))

        if listen_address == None:
            listen_address = Configuration.evaluation_server[0]
        if listen_port == None:
            listen_port = Configuration.evaluation_server[1]

        self.jd = JobDispatcher(len(Configuration.workers))
        self.st = threading.Thread()

        self.contest = contest
        self.contest.ranking_view = RankingView(contest)
        self.contest.update_ranking_view()
        self.contest.ranking_view.to_couch()
        self.contest.to_couch()
        for sub in self.contest.submissions:
            sub.invalid()
            self.add_job(sub.couch_id)

        RPCServer.__init__(self, "EvaluationServer", listen_address, listen_port,
                           [self.use_token,
                            self.add_job,
                            self.compilation_finished,
                            self.evaluation_finished,
                            self.self_destruct],
                           thread = self.st,
                           start_now = False)

    def start(self):
        self.jd.start()
        self.st.start()

    def use_token(self, submission_id):
        """Called by CWS when the user wants to use a token.

        If the evaluation of the submission is already in the queue,
        we increase its priority; otherwise, we do nothing, since the
        priority will be set as medium when we queue the evaluation.

        Instead, if we already evaluated the submission, we signal to
        the scorer. This is used by scorers that use both the last
        submission and the best tokenized submission to build the
        score.
        """
        submission = CouchObject.from_couch(submission_id)
        if submission.evaluation_outcome != None:
            submission.task.scorer.add_token(submission)
        self.jd.queue_set_priority((EvaluationServer.JOB_TYPE_EVALUATION, submission),
                                   EvaluationServer.JOB_PRIORITY_MEDIUM)
        return True

    def add_job(self, submission_id):
        self.contest.refresh()
        Utils.log("Queueing compilation for submission %s" % (submission_id))
        submission = CouchObject.from_couch(submission_id)
        self.jd.queue_push((EvaluationServer.JOB_TYPE_COMPILATION, submission),
                           EvaluationServer.JOB_PRIORITY_HIGH)
        return True

    def action_finished(self, job):
        worker = self.jd.find_worker(job)
        time_elapsed = time.time() - self.jd.workers.start_time[worker]
        Utils.log("Worker %d (%s:%d) finished to %s submission %s (took around %.2f seconds)" %
            (worker,
             Configuration.workers[worker][0],
             Configuration.workers[worker][1],
             job[0],
             job[1].couch_id,
             time_elapsed))
        self.jd.release_worker(worker)

    def compilation_finished(self, success, submission_id):
        """
        RPC method called by a Worker when a compilation has been
        completed.
        """
        submission = CouchObject.from_couch(submission_id)
        submission.compilation_tentatives += 1
        submission.to_couch()
        self.action_finished((EvaluationServer.JOB_TYPE_COMPILATION, submission))
        if success and submission.compilation_outcome == "ok":
            Utils.log("Compilation succeeded for submission %s, queueing evaluation" % (submission_id))
            priority = EvaluationServer.JOB_PRIORITY_LOW
            if submission.tokened():
                priority = EvaluationServer.JOB_PRIORITY_MEDIUM
            self.jd.queue_push((EvaluationServer.JOB_TYPE_EVALUATION, submission),
                               priority)
        elif success and submission.compilation_outcome == "fail":
            Utils.log("Compilation finished for submission %s, but the submission was not accepted; I'm not queueing evaluation" % (submission_id))
        else:
            Utils.log("Compilation failed for submission %s" % (submission_id))
            if submission.compilation_tentatives > \
                    EvaluationServer.MAX_COMPILATION_TENTATIVES:
                Utils.log("Maximum tentatives (%d) reached for the compilation of submission %s - I will not try again" %
                          (EvaluationServer.MAX_COMPILATION_TENTATIVES,
                           submission_id), Utils.Logger.SEVERITY_IMPORTANT)
            else:
                self.jd.queue_push((EvaluationServer.JOB_TYPE_COMPILATION, submission),
                                   EvaluationServer.JOB_PRIORITY_HIGH)
        return True

    def evaluation_finished(self, success, submission_id):
        """
        RPC method called by a Worker when an evaluation has been
        completed.
        """
        submission = CouchObject.from_couch(submission_id)
        submission.evaluation_tentatives += 1
        submission.to_couch()
        self.action_finished((EvaluationServer.JOB_TYPE_EVALUATION, submission))
        if success:
            Utils.log("Evaluation succeeded for submission %s" % (submission_id))
            self.update_evaluation(submission_id)
        else:
            Utils.log("Evaluation failed for submission %s" % (submission_id))
            if submission.evaluation_tentatives > \
                    EvaluationServer.MAX_EVALUATION_TENTATIVES:
                Utils.log("Maximum tentatives (%d) reached for the evaluation of submission %s - I will not try again" %
                          (EvaluationServer.MAX_EVALUATION_TENTATIVES,
                           submission_id), Utils.Logger.SEVERITY_IMPORTANT)
            else:
                # TODO - should check the original priority of the job
                self.jd.queue_push((EvaluationServer.JOB_TYPE_EVALUATION, submission),
                                   EvaluationServer.JOB_PRIORITY_LOW)
        return True

    def update_evaluation(self, submission_id):
        """
        Compute the evaluation for all submissions of the same task as
        submission_id's task, assuming that only submission_id has
        changed from last evaluation.
        """
        submission = CouchObject.from_couch(submission_id)
        scorer = submission.task.scorer
        scorer.add_submission(submission)
        self.contest.update_ranking_view()
        self.contest.ranking_view.to_couch()
        self.contest.to_couch()

    def self_destruct(self):
        self.jd.queue_push((EvaluationServer.JOB_TYPE_BOMB, None),
                           EvaluationServer.JOB_PRIORITY_EXTRA_HIGH)
        return True

def sigterm(signum, stack):
    global e
    print "Trying to self destruct"
    e.self_destruct()

if __name__ == "__main__":
    import sys

    es_address, es_port = Configuration.evaluation_server

    if sys.argv[1] == "run":
        Utils.set_service("evaluation server")
        c = Utils.ask_for_contest(skip = 1)
        e = EvaluationServer(c, es_address, es_port)
        e.start()
        signal.signal(signal.SIGTERM, sigterm)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            e.self_destruct()

    elif sys.argv[1] == "destroy":
        es = xmlrpclib.ServerProxy("http://localhost:%d" % es_port)
        es.self_destruct()

    elif sys.argv[1] == "submit":
        import Submission
        c = Utils.ask_for_contest(skip = 1)
        t = c.tasks[0]
        if len(sys.argv) >= 5:
            t = CouchObject.from_couch(sys.argv[4])
        s = Submission.sample_submission(user = c.users[0],
                                         task = t,
                                         files = [sys.argv[3]])
        c.submissions.append(s)
        c.to_couch()
        es = xmlrpclib.ServerProxy("http://localhost:%d" % es_port)
        es.add_job(s.couch_id)
