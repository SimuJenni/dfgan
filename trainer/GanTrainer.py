import os
import sys
import time
from datetime import datetime

import numpy as np
import tensorflow as tf
import tensorflow.contrib.gan as tfgan
from tensorflow.python.ops import control_flow_ops

from constants import LOG_DIR
from utils import get_checkpoint_path, get_variables_to_train, montage_tf, remove_missing, get_all_checkpoint_paths

slim = tf.contrib.slim


class GANTrainer:
    def __init__(self, model, dataset, num_train_steps=100000, optimizer_d='adam',
                 optimizer_g='adam', momentum=0.5, beta2=0.999, n_disc=1, lr=0.0001):
        tf.logging.set_verbosity(tf.logging.INFO)
        self.model = model
        self.dataset = dataset

        self.opt_d = optimizer_d
        self.opt_g = optimizer_g
        self.n_disc_steps = n_disc
        self.momentum = momentum
        self.beta2 = beta2
        self.lr = lr

        self.summaries = []
        self.moving_avgs_decay = 0.9999
        self.num_train_steps = None
        self.global_step = None
        self.num_train_steps = num_train_steps
        self.num_eval_steps = (self.dataset.num_test / self.model.batch_size)

    def get_save_dir(self):
        fname = '{}_{}'.format(self.model.name, self.dataset.name)
        return os.path.join(LOG_DIR, '{}/'.format(fname))

    def optimizer(self, opt_type):
        opts = {'adam': tf.train.AdamOptimizer(learning_rate=self.lr, beta1=self.momentum, beta2=self.beta2),
                'sgd': tf.train.GradientDescentOptimizer(learning_rate=self.lr),
                'momentum': tf.train.MomentumOptimizer(learning_rate=self.lr, momentum=self.momentum)}
        return opts[opt_type]

    def get_train_data_queue(self):
        print('Number of training steps: {}'.format(self.num_train_steps))
        imgs_, labels_ = self.dataset.get_data_train()

        imgs = tf.convert_to_tensor(imgs_, dtype=tf.float32)
        labels = tf.convert_to_tensor(labels_, dtype=tf.int32)
        data = tf.data.Dataset.from_tensor_slices((imgs, labels))

        data = data.repeat()
        data = data.shuffle(buffer_size=self.dataset.num_train)

        data = data.batch(self.model.batch_size)
        data = data.prefetch(100)
        iterator = data.make_one_shot_iterator()

        return iterator

    def get_test_data_queue(self):
        print('Number of evaluation steps: {}'.format(self.num_eval_steps))
        imgs_, labels_ = self.dataset.get_data_test()

        imgs = tf.convert_to_tensor(imgs_, dtype=tf.float32)
        labels = tf.convert_to_tensor(labels_, dtype=tf.int32)
        data = tf.data.Dataset.from_tensor_slices((imgs, labels))

        # create a new dataset with batches of images
        data = data.batch(self.model.batch_size)
        data = data.prefetch(100)
        iterator = data.make_one_shot_iterator()

        return iterator

    def make_summaries(self, grads, layers):
        # Variable summaries
        for variable in slim.get_model_variables():
            self.summaries.append(tf.summary.histogram(variable.op.name, variable))
        # Add histograms for gradients.
        for grad, var in grads:
            if grad is not None:
                self.summaries.append(tf.summary.histogram('gradients/' + var.op.name, grad))
        # Add histograms for activation.
        if layers:
            for layer_id, val in layers.iteritems():
                self.summaries.append(tf.summary.histogram('activations/' + layer_id, val))

    def get_noise_sample(self):
        noise_samples = tf.random_normal([self.model.batch_size, 128])
        return noise_samples

    def build_generator(self, batch_queue, opt, scope):
        noise_samples = self.get_noise_sample()
        fake_imgs = self.model.gen(noise_samples)

        # Create the model
        preds_fake = self.model.disc(fake_imgs)

        # Compute losses
        loss = self.model.g_loss(scope, preds_fake)
        tf.get_variable_scope().reuse_variables()

        # Handle dependencies with update_ops (batch-norm)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        if update_ops:
            updates = tf.group(*update_ops)
            loss = control_flow_ops.with_dependencies([updates], loss)

        # Calculate the gradients for the batch of data on this tower.
        grads = opt.compute_gradients(loss, get_variables_to_train('generator'))

        self.summaries += tf.get_collection(tf.GraphKeys.SUMMARIES, scope)
        return loss, grads, {}

    def build_discriminator(self, batch_queue, opt, scope):
        imgs_train, _ = batch_queue.get_next()
        imgs_train.set_shape([self.model.batch_size, ] + self.model.im_shape)

        noise_samples = self.get_noise_sample()
        fake_imgs = self.model.gen(noise_samples)
        tf.summary.image('imgs/train', montage_tf(imgs_train, 4, 16), max_outputs=1)
        tf.summary.image('imgs/fake', montage_tf(fake_imgs, 4, 16), max_outputs=1)

        preds_fake = self.model.disc(fake_imgs, reuse=None)
        preds_real = self.model.disc(imgs_train, reuse=True)

        # Compute losses
        loss = self.model.d_loss(scope, preds_fake, preds_real)
        tf.get_variable_scope().reuse_variables()

        # Handle dependencies with update_ops (batch-norm)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        if update_ops:
            updates = tf.group(*update_ops)
            loss = control_flow_ops.with_dependencies([updates], loss)

        # Calculate the gradients for the batch of data on this tower.
        grads = opt.compute_gradients(loss, get_variables_to_train('discriminator'))

        self.summaries += tf.get_collection(tf.GraphKeys.SUMMARIES, scope)
        return loss, grads, {}

    def make_init_fn(self, chpt_path):
        if chpt_path is None:
            return None

        var2restore = slim.get_variables_to_restore(include=['discriminator', 'generator'])

        print('Variables to restore: {}'.format([v.op.name for v in var2restore]))
        var2restore = remove_missing(var2restore, chpt_path)
        init_assign_op, init_feed_dict = slim.assign_from_checkpoint(chpt_path, var2restore)
        sys.stdout.flush()

        # Create an initial assignment function.
        def init_fn(sess):
            print('Restoring from: {}'.format(chpt_path))
            sess.run(init_assign_op, init_feed_dict)

        return init_fn

    def train_model(self, chpt_path):
        print('Restoring from: {}'.format(chpt_path))
        g = tf.Graph()
        with g.as_default():
            with tf.device('/cpu:0'):
                tf.random.set_random_seed(123)

                # Init global step
                self.global_step = tf.train.create_global_step()

                batch_queue = self.get_train_data_queue()
                opt_d = self.optimizer(self.opt_d)
                opt_g = self.optimizer(self.opt_g)

                # Calculate the gradients for each model tower.
                with tf.variable_scope(tf.get_variable_scope()):
                    with tf.device('/gpu:%d' % 0):
                        with tf.name_scope('gen') as scope:
                            loss_g, grad_g, layers_g = self.build_generator(batch_queue, opt_g, scope)
                        with tf.name_scope('disc') as scope:
                            loss_d, grad_d, layers_d = self.build_discriminator(batch_queue, opt_d, scope)

                # Make summaries
                self.make_summaries(grad_d + grad_g, layers_d)

                # Apply the gradients to adjust the shared variables.
                apply_gradient_op_d = opt_d.apply_gradients(grad_d, global_step=self.global_step)
                apply_gradient_op_g = opt_g.apply_gradients(grad_g, global_step=self.global_step)

                # Track the moving averages of all trainable variables.
                variable_averages = tf.train.ExponentialMovingAverage(self.moving_avgs_decay, self.global_step)
                variables_averages_op = variable_averages.apply(tf.trainable_variables())

                # Group all updates to into a single train op.
                apply_gradient_op_d = tf.group(apply_gradient_op_d, variables_averages_op)
                apply_gradient_op_g = tf.group(apply_gradient_op_g, variables_averages_op)
                train_op_d = control_flow_ops.with_dependencies([apply_gradient_op_d], loss_d)
                train_op_g = control_flow_ops.with_dependencies([apply_gradient_op_g], loss_g)

                # Create a saver.
                saver = tf.train.Saver(tf.global_variables())
                init_fn = self.make_init_fn(chpt_path)

                # Build the summary operation from the last tower summaries.
                summary_op = tf.summary.merge(self.summaries)

                # Build an initialization operation to run below.
                init = tf.global_variables_initializer()

                # Start running operations on the Graph.
                sess = tf.Session(config=tf.ConfigProto(
                    allow_soft_placement=True,
                    log_device_placement=False), graph=g)
                sess.run(init)
                prev_ckpt = get_checkpoint_path(self.get_save_dir())
                if prev_ckpt:
                    print('Restoring from previous checkpoint: {}'.format(prev_ckpt))
                    saver.restore(sess, prev_ckpt)
                elif init_fn:
                    init_fn(sess)

                # Start the queue runners.
                tf.train.start_queue_runners(sess=sess)

                summary_writer = tf.summary.FileWriter(self.get_save_dir(), sess.graph)
                init_step = sess.run(self.global_step)
                init_step /= (1 + self.n_disc_steps)
                print('Start training at step: {}'.format(init_step))
                for step in range(init_step, self.num_train_steps):

                    start_time = time.time()
                    for i in range(self.n_disc_steps):
                        _, loss_value = sess.run([train_op_d, loss_d])
                    _, loss_value = sess.run([train_op_g, loss_g])

                    duration = time.time() - start_time

                    assert not np.isnan(loss_value), 'Model diverged with loss = NaN'

                    if step % (self.num_train_steps / 2000) == 0:
                        num_examples_per_step = self.model.batch_size
                        examples_per_sec = num_examples_per_step / duration
                        sec_per_batch = duration
                        print('{}: step {}/{}, loss = {} ({} examples/sec; {} sec/batch)'
                              .format(datetime.now(), step, self.num_train_steps, loss_value,
                                      examples_per_sec, sec_per_batch))
                        sys.stdout.flush()

                    if step % (self.num_train_steps / 200) == 0:
                        print('Writing summaries...')
                        summary_str = sess.run(summary_op)
                        summary_writer.add_summary(summary_str, step)

                    # Save the model checkpoint periodically.
                    if step % (self.num_train_steps / 40) == 0 or (step + 1) == self.num_train_steps:
                        checkpoint_path = os.path.join(self.get_save_dir(), 'model.ckpt')
                        print('Saving checkpoint to: {}'.format(checkpoint_path))
                        saver.save(sess, checkpoint_path, global_step=step)

    def test_gan_all(self, num_comp=10000):
        fids = []
        is_s = []
        ckpts = get_all_checkpoint_paths(self.get_save_dir())
        for c in ckpts:
            fid, is_ = self.test_gan(num_comp, ckpt=c)
            print('FID: {}  IS: {}'.format(fid, is_))
            fids.append(fid)
            is_s.append(is_)
        return '{}+-{}'.format(np.mean(fids), np.std(fids)), '{}+-{}'.format(np.mean(is_s), np.std(is_s))

    def test_gan(self, num_comp=10000, ckpt=None):
        if not ckpt:
            ckpt = get_checkpoint_path(self.get_save_dir())
        f_act, r_act, f_log = self.get_activations(num_comp, ckpt)

        g = tf.Graph()
        with g.as_default():
            # Placeholders for FID
            a_shape = f_act.shape
            real_acts = tf.placeholder(tf.float32, shape=a_shape)
            fake_acts = tf.placeholder(tf.float32, shape=a_shape)
            l_shape = f_log.shape
            fake_logs = tf.placeholder(tf.float32, shape=l_shape)

            # Compute Frechet Inception Distance.
            fid = tfgan.eval.frechet_classifier_distance_from_activations(
                real_acts, fake_acts)
            i_s = tfgan.eval.classifier_score_from_logits(fake_logs)
            sess = tf.Session(graph=g)
            return sess.run([fid, i_s], feed_dict={real_acts: r_act, fake_acts: f_act, fake_logs: f_log})

    def get_activations(self, num_comp, ckpt):
        g = tf.Graph()
        with g.as_default():
            batch_queue = self.get_test_data_queue()
            imgs_train, _ = batch_queue.get_next()
            imgs_train.set_shape([self.model.batch_size, ] + self.model.im_shape)

            noise_samples = self.get_noise_sample()
            fake_imgs_ = self.model.gen(noise_samples)

            # Resize input images.
            size = 299
            resized_real_images = tf.image.resize_bilinear(imgs_train, [size, size])
            resized_generated_images = tf.image.resize_bilinear(fake_imgs_, [size, size])

            fake_act = tfgan.eval.run_inception(resized_generated_images, output_tensor='pool_3:0')
            real_act = tfgan.eval.run_inception(resized_real_images, output_tensor='pool_3:0')

            fake_log = tfgan.eval.run_inception(resized_generated_images, output_tensor='logits:0')

            sess = tf.Session(graph=g)

            # Build an initialization operation to run below.
            init = tf.global_variables_initializer()
            sess.run(init)
            print('Restoring from previous checkpoint: {}'.format(ckpt))
            ema = tf.train.ExponentialMovingAverage(self.moving_avgs_decay, self.global_step)
            variables_to_restore = ema.variables_to_restore()
            saver = tf.train.Saver(variables_to_restore)
            saver.restore(sess, ckpt)

            # Collect num_comp real and fake images
            f_acts = []
            r_acts = []
            f_logs = []
            for i in range(num_comp // self.model.batch_size):
                f_a, r_a, f_l = sess.run([fake_act, real_act, fake_log])
                f_acts.append(f_a)
                r_acts.append(r_a)
                f_logs.append(f_l)

            f_acts = np.concatenate(f_acts)
            r_acts = np.concatenate(r_acts)
            f_logs = np.concatenate(f_logs)

            sess.close()

            return f_acts, r_acts, f_logs
