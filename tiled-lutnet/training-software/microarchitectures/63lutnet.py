import numpy as np
import pickle
import matplotlib.pyplot as plt
import matplotlib

import tensorflow as tf
import keras
from keras.models import Sequential, Model
from keras.layers import Dense, Convolution2D, Activation, Flatten, MaxPooling2D,Input,Dropout,GlobalAveragePooling2D
from keras import backend as K
from keras.datasets import cifar10
from keras.utils import np_utils
from keras.optimizers import SGD
from keras.engine.topology import Layer
from keras.models import load_model
from keras.preprocessing.image import ImageDataGenerator
import os
from keras.layers.normalization import BatchNormalization
from tensorflow.python.framework import ops
#from multi_gpu import make_parallel

def binarize(x):
    '''Element-wise rounding to the closest integer with full gradient propagation.
    A trick from [Sergey Ioffe](http://stackoverflow.com/a/36480182)
    '''
    clipped = K.clip(x,-1,1)
    rounded = K.sign(clipped)
    return clipped + K.stop_gradient(rounded - clipped)

class Residual_sign(Layer):
    def __init__(self, levels=1,trainable=True,**kwargs):
        self.levels=levels
        self.trainable=trainable
        super(Residual_sign, self).__init__(**kwargs)
    def build(self, input_shape):
        ars=np.arange(self.levels)+1.0
        ars=ars[::-1]
        means=ars/np.sum(ars)
        #self.means=[K.variable(m) for m in means]
        #self.trainable_weights=self.means
        self.means = self.add_weight(name='means',
            shape=(self.levels, ),
            initializer=keras.initializers.Constant(value=means),
            trainable=self.trainable) # Trainable scaling factors for residual binarisation
    def call(self, x, mask=None):
        resid = x
        out_bin=0

        if self.levels==1:
            for l in range(self.levels):
                #out=binarize(resid)*K.abs(self.means[l])
                out=binarize(resid)*abs(self.means[l])
                #out_bin=out_bin+out
                out_bin=out_bin+out#no gamma per level
                resid=resid-out
        elif self.levels==2:
            out=binarize(resid)*abs(self.means[0])
            out_bin=out
            resid=resid-out
            out=binarize(resid)*abs(self.means[1])
            out_bin=tf.stack([out_bin,out])
            resid=resid-out
        elif self.levels==3:
            out=binarize(resid)*abs(self.means[0])
            out_bin1=out
            resid=resid-out
            out=binarize(resid)*abs(self.means[1])
            out_bin2=out
            resid=resid-out
            out=binarize(resid)*abs(self.means[2])
            out_bin3=out
            resid=resid-out
            out_bin=tf.stack([out_bin1,out_bin2,out_bin3])

                
        return out_bin

    def get_output_shape_for(self,input_shape):
        if self.levels==1:
            return input_shape
        else:
            return (self.levels, input_shape)
    def compute_output_shape(self,input_shape):
        if self.levels==1:
            return input_shape
        else:
            return (self.levels, input_shape)
    def set_means(self,X):
        means=np.zeros((self.levels))
        means[0]=1
        resid=np.clip(X,-1,1)
        approx=0
        for l in range(self.levels):
            m=np.mean(np.absolute(resid))
            out=np.sign(resid)*m
            approx=approx+out
            resid=resid-out
            means[l]=m
            err=np.mean((approx-np.clip(X,-1,1))**2)

        means=means/np.sum(means)
        sess=K.get_session()
        sess.run(self.means.assign(means))

class binary_conv(Layer):
	def __init__(self,nfilters,ch_in,k,padding,strides=(1,1),levels=1,pruning_prob=0,first_layer=False,LUT=True,BINARY=True,TRC=1,TM=1,TN=1,**kwargs):
		self.nfilters=nfilters
		self.ch_in=ch_in
		self.k=k
		self.padding=padding
		if padding=='valid':
			self.PADDING = "VALID" #tf uses upper-case padding notations whereas keras uses lower-case notations
		elif padding=='same':
			self.PADDING = "SAME"
		self.strides=strides
		self.levels=levels
		self.first_layer=first_layer
		self.LUT=LUT
		self.BINARY=BINARY
		self.window_size=self.ch_in*self.k*self.k
		self.TRC = TRC
		self.TM = TM
		self.TN = TN
		self.tile_size=[self.k/self.TRC,self.k/self.TRC,self.ch_in/self.TM,self.nfilters/self.TN]
		#self.rand_map=np.random.randint(self.window_size, size=[self.window_size, 1]) # Randomisation map for subsequent input connections
		super(binary_conv,self).__init__(**kwargs)
	def build(self, input_shape):

		self.rand_map_0 = self.add_weight(name='rand_map_0', 
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], size=[self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1])),
			trainable=False) # Randomisation map for subsequent input connections
		self.rand_map_1 = self.add_weight(name='rand_map_1', 
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], size=[self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1])),
			trainable=False) # Randomisation map for subsequent input connections

		self.rand_map_exp_0 = self.add_weight(name='rand_map_exp_0', 
			shape=(self.window_size, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.window_size, size=[self.window_size, 1])),
			trainable=False) # Randomisation map for subsequent input connections
		self.rand_map_exp_1 = self.add_weight(name='rand_map_exp_1', 
			shape=(self.window_size, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.window_size, size=[self.window_size, 1])),
			trainable=False) # Randomisation map for subsequent input connections

		stdv=1/np.sqrt(self.k*self.k*self.ch_in)
		self.gamma=K.variable(1.0)
#		if self.first_layer==True:
#			w1 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
#			w2 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
#			w3 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
#			w4 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
#
#			self.w1=K.variable(w1)
#			self.w2=K.variable(w2)
#			self.w3=K.variable(w3)
#			self.w4=K.variable(w4)
#			self.trainable_weights=[self.w1,self.w2,self.w3,self.w4,self.gamma]

		if self.levels==1 or self.first_layer==True:
			w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
			self.w=K.variable(w)
			self.trainable_weights=[self.w,self.gamma]
		elif self.levels==2:
			if self.LUT==True:

				w1  = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w2  = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w3  = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				c1  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c2  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c3  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c4  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c5  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c6  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c7  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c8  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c9  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c10 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c11 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c12 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c13 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c14 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c15 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c16 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c17 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c18 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c19 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c20 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c21 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c22 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c23 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c24 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c25 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c26 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c27 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c28 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c29 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c30 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c31 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c32 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c33 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c34 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c35 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c36 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c37 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c38 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c39 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c40 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c41 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c42 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c43 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c44 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c45 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c46 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c47 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c48 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c49 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c50 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c51 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c52 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c53 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c54 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c55 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c56 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c57 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c58 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c59 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c60 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c61 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c62 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c63 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c64 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
			
#				self.w1 = self.add_weight(name='w1', 
#					shape=(self.k,self.k,self.ch_in,self.nfilters),
#					initializer=keras.initializers.Constant(value=np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)),
#					trainable=False) 
#				self.w2 = self.add_weight(name='w2', 
#					shape=(self.k,self.k,self.ch_in,self.nfilters),
#					initializer=keras.initializers.Constant(value=np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)),
#					trainable=False) 
#				self.w3 = self.add_weight(name='w3', 
#					shape=(self.k,self.k,self.ch_in,self.nfilters),
#					initializer=keras.initializers.Constant(value=np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)),
#					trainable=False) 

				self.c1 =K.variable(c1)
				self.c2 =K.variable(c2)
				self.c3 =K.variable(c3)
				self.c4 =K.variable(c4)
				self.c5 =K.variable(c5)
				self.c6 =K.variable(c6)
				self.c7 =K.variable(c7)
				self.c8 =K.variable(c8)
				self.c9 =K.variable(c9)
				self.c10=K.variable(c10)
				self.c11=K.variable(c11)
				self.c12=K.variable(c12)
				self.c13=K.variable(c13)
				self.c14=K.variable(c14)
				self.c15=K.variable(c15)
				self.c16=K.variable(c16)
				self.c17=K.variable(c17)
				self.c18=K.variable(c18)
				self.c19=K.variable(c19)
				self.c20=K.variable(c20)
				self.c21=K.variable(c21)
				self.c22=K.variable(c22)
				self.c23=K.variable(c23)
				self.c24=K.variable(c24)
				self.c25=K.variable(c25)
				self.c26=K.variable(c26)
				self.c27=K.variable(c27)
				self.c28=K.variable(c28)
				self.c29=K.variable(c29)
				self.c30=K.variable(c30)
				self.c31=K.variable(c31)
				self.c32=K.variable(c32)
				self.c33=K.variable(c33)
				self.c34=K.variable(c34)
				self.c35=K.variable(c35)
				self.c36=K.variable(c36)
				self.c37=K.variable(c37)
				self.c38=K.variable(c38)
				self.c39=K.variable(c39)
				self.c40=K.variable(c40)
				self.c41=K.variable(c41)
				self.c42=K.variable(c42)
				self.c43=K.variable(c43)
				self.c44=K.variable(c44)
				self.c45=K.variable(c45)
				self.c46=K.variable(c46)
				self.c47=K.variable(c47)
				self.c48=K.variable(c48)
				self.c49=K.variable(c49)
				self.c50=K.variable(c50)
				self.c51=K.variable(c51)
				self.c52=K.variable(c52)
				self.c53=K.variable(c53)
				self.c54=K.variable(c54)
				self.c55=K.variable(c55)
				self.c56=K.variable(c56)
				self.c57=K.variable(c57)
				self.c58=K.variable(c58)
				self.c59=K.variable(c59)
				self.c60=K.variable(c60)
				self.c61=K.variable(c61)
				self.c62=K.variable(c62)
				self.c63=K.variable(c63)
				self.c64=K.variable(c64)
				self.w1 =K.variable(w1)
				self.w2 =K.variable(w2)
				self.w3 =K.variable(w3)

				self.trainable_weights=[self.c1,self.c2,self.c3,self.c4,self.c5,self.c6,self.c7,self.c8,self.c9,self.c10,self.c11,self.c12,self.c13,self.c14,self.c15,self.c16,
					self.c17,self.c18,self.c19,self.c20,self.c21,self.c22,self.c23,self.c24,self.c25,self.c26,self.c27,self.c28,self.c29,self.c30,self.c31,self.c32,
					self.c33,self.c34,self.c35,self.c36,self.c37,self.c38,self.c39,self.c40,self.c41,self.c42,self.c43,self.c44,self.c45,self.c46,self.c47,self.c48,
					self.c49,self.c50,self.c51,self.c52,self.c53,self.c54,self.c55,self.c56,self.c57,self.c58,self.c59,self.c60,self.c61,self.c62,self.c63,self.c64,
					self.w1,self.w2,self.w3,self.gamma]

			else:
				w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				self.w=K.variable(w)
				self.trainable_weights=[self.w,self.gamma]
	

		elif self.levels==3:
			if self.LUT==True:
				w1 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w2 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w3 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w4 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w5 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w6 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w7 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				w8 = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				self.w1=K.variable(w1)
				self.w2=K.variable(w2)
				self.w3=K.variable(w3)
				self.w4=K.variable(w4)
				self.w5=K.variable(w5)
				self.w6=K.variable(w6)
				self.w7=K.variable(w7)
				self.w8=K.variable(w8)
				self.trainable_weights=[self.w1,self.w2,self.w3,self.w4,self.w5,self.w6,self.w7,self.w8,self.gamma]
			else:
				w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				self.w=K.variable(w)
				self.trainable_weights=[self.w,self.gamma]

		self.pruning_mask = self.add_weight(name='pruning_mask',
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2],self.tile_size[3]),
			initializer=keras.initializers.Constant(value=np.ones((self.tile_size[0]*self.tile_size[1]*self.tile_size[2],self.tile_size[3]))),
			trainable=False) # LUT pruning based on whether inputs get repeated



#		if keras.backend._backend=="mxnet":
#			w=w.transpose(3,2,0,1)

#		if self.levels==1:#train baseline with no resid gamma scaling
#			w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
#			self.w=K.variable(w)
#			self.trainable_weights=[self.w,self.gamma]
#		elif self.levels==2:
#			w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
#			self.w=K.variable(w)
#			self.trainable_weights=[self.w,self.gamma]



	def call(self, x,mask=None):
		constraint_gamma=K.abs(self.gamma)#K.clip(self.gamma,0.01,10)

		if self.levels==1 or self.first_layer==True:
			if self.BINARY==False:
				self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
			else:
				self.clamped_w=constraint_gamma*binarize(self.w)
		elif self.levels==2:
			if self.LUT==True:
				if self.BINARY==False:
					self.clamped_w1 =K.clip(self.w1,-1,1)
					self.clamped_w2 =K.clip(self.w2,-1,1)
					self.clamped_w3 =K.clip(self.w3,-1,1)

					self.clamped_c1 =constraint_gamma*K.clip(tf.tile(self.c1,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c2 =constraint_gamma*K.clip(tf.tile(self.c2,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c3 =constraint_gamma*K.clip(tf.tile(self.c3,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c4 =constraint_gamma*K.clip(tf.tile(self.c4,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c5 =constraint_gamma*K.clip(tf.tile(self.c5,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c6 =constraint_gamma*K.clip(tf.tile(self.c6,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c7 =constraint_gamma*K.clip(tf.tile(self.c7,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c8 =constraint_gamma*K.clip(tf.tile(self.c8,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c9 =constraint_gamma*K.clip(tf.tile(self.c9,  [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c10=constraint_gamma*K.clip(tf.tile(self.c10, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c11=constraint_gamma*K.clip(tf.tile(self.c11, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c12=constraint_gamma*K.clip(tf.tile(self.c12, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c13=constraint_gamma*K.clip(tf.tile(self.c13, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c14=constraint_gamma*K.clip(tf.tile(self.c14, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c15=constraint_gamma*K.clip(tf.tile(self.c15, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c16=constraint_gamma*K.clip(tf.tile(self.c16, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c17=constraint_gamma*K.clip(tf.tile(self.c17, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c18=constraint_gamma*K.clip(tf.tile(self.c18, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c19=constraint_gamma*K.clip(tf.tile(self.c19, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c20=constraint_gamma*K.clip(tf.tile(self.c20, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c21=constraint_gamma*K.clip(tf.tile(self.c21, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c22=constraint_gamma*K.clip(tf.tile(self.c22, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c23=constraint_gamma*K.clip(tf.tile(self.c23, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c24=constraint_gamma*K.clip(tf.tile(self.c24, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c25=constraint_gamma*K.clip(tf.tile(self.c25, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c26=constraint_gamma*K.clip(tf.tile(self.c26, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c27=constraint_gamma*K.clip(tf.tile(self.c27, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c28=constraint_gamma*K.clip(tf.tile(self.c28, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c29=constraint_gamma*K.clip(tf.tile(self.c29, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c30=constraint_gamma*K.clip(tf.tile(self.c30, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c31=constraint_gamma*K.clip(tf.tile(self.c31, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c32=constraint_gamma*K.clip(tf.tile(self.c32, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c33=constraint_gamma*K.clip(tf.tile(self.c33, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c34=constraint_gamma*K.clip(tf.tile(self.c34, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c35=constraint_gamma*K.clip(tf.tile(self.c35, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c36=constraint_gamma*K.clip(tf.tile(self.c36, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c37=constraint_gamma*K.clip(tf.tile(self.c37, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c38=constraint_gamma*K.clip(tf.tile(self.c38, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c39=constraint_gamma*K.clip(tf.tile(self.c39, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c40=constraint_gamma*K.clip(tf.tile(self.c40, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c41=constraint_gamma*K.clip(tf.tile(self.c41, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c42=constraint_gamma*K.clip(tf.tile(self.c42, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c43=constraint_gamma*K.clip(tf.tile(self.c43, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c44=constraint_gamma*K.clip(tf.tile(self.c44, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c45=constraint_gamma*K.clip(tf.tile(self.c45, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c46=constraint_gamma*K.clip(tf.tile(self.c46, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c47=constraint_gamma*K.clip(tf.tile(self.c47, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c48=constraint_gamma*K.clip(tf.tile(self.c48, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c49=constraint_gamma*K.clip(tf.tile(self.c49, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c50=constraint_gamma*K.clip(tf.tile(self.c50, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c51=constraint_gamma*K.clip(tf.tile(self.c51, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c52=constraint_gamma*K.clip(tf.tile(self.c52, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c53=constraint_gamma*K.clip(tf.tile(self.c53, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c54=constraint_gamma*K.clip(tf.tile(self.c54, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c55=constraint_gamma*K.clip(tf.tile(self.c55, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c56=constraint_gamma*K.clip(tf.tile(self.c56, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c57=constraint_gamma*K.clip(tf.tile(self.c57, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c58=constraint_gamma*K.clip(tf.tile(self.c58, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c59=constraint_gamma*K.clip(tf.tile(self.c59, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c60=constraint_gamma*K.clip(tf.tile(self.c60, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c61=constraint_gamma*K.clip(tf.tile(self.c61, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c62=constraint_gamma*K.clip(tf.tile(self.c62, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c63=constraint_gamma*K.clip(tf.tile(self.c63, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)
					self.clamped_c64=constraint_gamma*K.clip(tf.tile(self.c64, [self.TRC,self.TRC,self.TM,self.TN]),-1,1)

				else:

					self.clamped_w1 =binarize(self.w1)
					self.clamped_w2 =binarize(self.w2)
					self.clamped_w3 =binarize(self.w3)

					self.clamped_c1 =constraint_gamma*binarize(tf.tile(self.c1, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c2 =constraint_gamma*binarize(tf.tile(self.c2, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c3 =constraint_gamma*binarize(tf.tile(self.c3, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c4 =constraint_gamma*binarize(tf.tile(self.c4, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c5 =constraint_gamma*binarize(tf.tile(self.c5, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c6 =constraint_gamma*binarize(tf.tile(self.c6, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c7 =constraint_gamma*binarize(tf.tile(self.c7, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c8 =constraint_gamma*binarize(tf.tile(self.c8, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c9 =constraint_gamma*binarize(tf.tile(self.c9, [self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c10=constraint_gamma*binarize(tf.tile(self.c10,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c11=constraint_gamma*binarize(tf.tile(self.c11,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c12=constraint_gamma*binarize(tf.tile(self.c12,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c13=constraint_gamma*binarize(tf.tile(self.c13,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c14=constraint_gamma*binarize(tf.tile(self.c14,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c15=constraint_gamma*binarize(tf.tile(self.c15,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c16=constraint_gamma*binarize(tf.tile(self.c16,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c17=constraint_gamma*binarize(tf.tile(self.c17,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c18=constraint_gamma*binarize(tf.tile(self.c18,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c19=constraint_gamma*binarize(tf.tile(self.c19,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c20=constraint_gamma*binarize(tf.tile(self.c20,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c21=constraint_gamma*binarize(tf.tile(self.c21,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c22=constraint_gamma*binarize(tf.tile(self.c22,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c23=constraint_gamma*binarize(tf.tile(self.c23,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c24=constraint_gamma*binarize(tf.tile(self.c24,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c25=constraint_gamma*binarize(tf.tile(self.c25,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c26=constraint_gamma*binarize(tf.tile(self.c26,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c27=constraint_gamma*binarize(tf.tile(self.c27,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c28=constraint_gamma*binarize(tf.tile(self.c28,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c29=constraint_gamma*binarize(tf.tile(self.c29,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c30=constraint_gamma*binarize(tf.tile(self.c30,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c31=constraint_gamma*binarize(tf.tile(self.c31,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c32=constraint_gamma*binarize(tf.tile(self.c32,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c33=constraint_gamma*binarize(tf.tile(self.c33,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c34=constraint_gamma*binarize(tf.tile(self.c34,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c35=constraint_gamma*binarize(tf.tile(self.c35,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c36=constraint_gamma*binarize(tf.tile(self.c36,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c37=constraint_gamma*binarize(tf.tile(self.c37,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c38=constraint_gamma*binarize(tf.tile(self.c38,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c39=constraint_gamma*binarize(tf.tile(self.c39,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c40=constraint_gamma*binarize(tf.tile(self.c40,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c41=constraint_gamma*binarize(tf.tile(self.c41,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c42=constraint_gamma*binarize(tf.tile(self.c42,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c43=constraint_gamma*binarize(tf.tile(self.c43,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c44=constraint_gamma*binarize(tf.tile(self.c44,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c45=constraint_gamma*binarize(tf.tile(self.c45,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c46=constraint_gamma*binarize(tf.tile(self.c46,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c47=constraint_gamma*binarize(tf.tile(self.c47,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c48=constraint_gamma*binarize(tf.tile(self.c48,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c49=constraint_gamma*binarize(tf.tile(self.c49,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c50=constraint_gamma*binarize(tf.tile(self.c50,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c51=constraint_gamma*binarize(tf.tile(self.c51,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c52=constraint_gamma*binarize(tf.tile(self.c52,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c53=constraint_gamma*binarize(tf.tile(self.c53,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c54=constraint_gamma*binarize(tf.tile(self.c54,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c55=constraint_gamma*binarize(tf.tile(self.c55,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c56=constraint_gamma*binarize(tf.tile(self.c56,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c57=constraint_gamma*binarize(tf.tile(self.c57,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c58=constraint_gamma*binarize(tf.tile(self.c58,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c59=constraint_gamma*binarize(tf.tile(self.c59,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c60=constraint_gamma*binarize(tf.tile(self.c60,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c61=constraint_gamma*binarize(tf.tile(self.c61,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c62=constraint_gamma*binarize(tf.tile(self.c62,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c63=constraint_gamma*binarize(tf.tile(self.c63,[self.TRC,self.TRC,self.TM,self.TN]))
					self.clamped_c64=constraint_gamma*binarize(tf.tile(self.c64,[self.TRC,self.TRC,self.TM,self.TN]))

			else:
				if self.BINARY==False:
					self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
				else:
					self.clamped_w=constraint_gamma*binarize(self.w)
		elif self.levels==3:
			if self.LUT==True:
				self.clamped_w1=constraint_gamma*binarize(self.w1)
				self.clamped_w2=constraint_gamma*binarize(self.w2)
				self.clamped_w3=constraint_gamma*binarize(self.w3)
				self.clamped_w4=constraint_gamma*binarize(self.w4)
				self.clamped_w5=constraint_gamma*binarize(self.w5)
				self.clamped_w6=constraint_gamma*binarize(self.w6)
				self.clamped_w7=constraint_gamma*binarize(self.w7)
				self.clamped_w8=constraint_gamma*binarize(self.w8)

			else:
				self.clamped_w=constraint_gamma*binarize(self.w)


#		if self.levels==1:#train baseline with no resid gamma scaling
#			self.clamped_w=constraint_gamma*binarize(self.w)
#			#self.clamped_w=binarize(self.w)#no gamma per weight channel
#		elif self.levels==2:
#			self.clamped_w=constraint_gamma*binarize(self.w)

		if keras.__version__[0]=='2':

			if self.levels==1 or self.first_layer==True:
				self.out=K.conv2d(x, kernel=self.clamped_w*tf.tile(tf.reshape(self.pruning_mask, self.tile_size), [self.TRC,self.TRC,self.TM,self.TN]), padding=self.padding,strides=self.strides )
			elif self.levels==2:
				if self.LUT==True:
					x0_patches = tf.extract_image_patches(x[0,:,:,:,:],
						[1, self.k, self.k, 1],
						[1, self.strides[0], self.strides[1], 1], [1, 1, 1, 1],
						padding=self.PADDING)
					x1_patches = tf.extract_image_patches(x[1,:,:,:,:],
						[1, self.k, self.k, 1],
						[1, self.strides[0], self.strides[1], 1], [1, 1, 1, 1],
						padding=self.PADDING)
					# Special hack for randomising the subsequent input connections: tensorflow does not support advanced matrix indexing
					x0_shuf_patches=tf.transpose(x0_patches, perm=[3, 0, 1, 2])
					x0_shuf_patches_0 = tf.gather_nd(x0_shuf_patches, tf.cast(self.rand_map_exp_0, tf.int32))
					x0_shuf_patches_0=tf.transpose(x0_shuf_patches_0, perm=[1, 2, 3, 0])
					x0_shuf_patches_1 = tf.gather_nd(x0_shuf_patches, tf.cast(self.rand_map_exp_1, tf.int32))
					x0_shuf_patches_1=tf.transpose(x0_shuf_patches_1, perm=[1, 2, 3, 0])

					x1_shuf_patches=tf.transpose(x1_patches, perm=[3, 0, 1, 2])
					x1_shuf_patches_0 = tf.gather_nd(x1_shuf_patches, tf.cast(self.rand_map_exp_0, tf.int32))
					x1_shuf_patches_0=tf.transpose(x1_shuf_patches_0, perm=[1, 2, 3, 0])
					x1_shuf_patches_1 = tf.gather_nd(x1_shuf_patches, tf.cast(self.rand_map_exp_1, tf.int32))
					x1_shuf_patches_1=tf.transpose(x1_shuf_patches_1, perm=[1, 2, 3, 0])

					x0_pos=(1+binarize(x0_patches))/2*abs(x0_patches)
					x0_neg=(1-binarize(x0_patches))/2*abs(x0_patches)
					x1_pos=(1+binarize(x1_patches))/2*abs(x1_patches)
					x1_neg=(1-binarize(x1_patches))/2*abs(x1_patches)
					x0s0_pos=(1+binarize(x0_shuf_patches_0))/2#*abs(x0_shuf_patches_0)
					x0s0_neg=(1-binarize(x0_shuf_patches_0))/2#*abs(x0_shuf_patches_0)
					x1s0_pos=(1+binarize(x1_shuf_patches_0))/2#*abs(x1_shuf_patches_0)
					x1s0_neg=(1-binarize(x1_shuf_patches_0))/2#*abs(x1_shuf_patches_0)
					x0s1_pos=(1+binarize(x0_shuf_patches_1))/2#*abs(x0_shuf_patches_0)
					x0s1_neg=(1-binarize(x0_shuf_patches_1))/2#*abs(x0_shuf_patches_0)
					x1s1_pos=(1+binarize(x1_shuf_patches_1))/2#*abs(x1_shuf_patches_0)
					x1s1_neg=(1-binarize(x1_shuf_patches_1))/2#*abs(x1_shuf_patches_0)

					ws0_pos=(1+binarize(self.clamped_w1))/2
					ws0_neg=(1-binarize(self.clamped_w1))/2
					ws1_pos=(1+binarize(self.clamped_w2))/2
					ws1_neg=(1-binarize(self.clamped_w2))/2
					ws2_pos=(1+binarize(self.clamped_w3))/2
					ws2_neg=(1-binarize(self.clamped_w3))/2

					self.out=         K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c1 *ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c2 *ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c3 *ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c4 *ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c5 *ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c6 *ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c7 *ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c8 *ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c9 *ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c10*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c11*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c12*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c13*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c14*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c15*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c16*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c17*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c18*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c19*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c20*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c21*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c22*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c23*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c24*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c25*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c26*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c27*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c28*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c29*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c30*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c31*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c32*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c33*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c34*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c35*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c36*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c37*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c38*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c39*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos, tf.reshape(self.clamped_c40*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c41*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c42*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c43*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c44*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c45*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c46*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c47*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg, tf.reshape(self.clamped_c48*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c49*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c50*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c51*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c52*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c53*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c54*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c55*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos, tf.reshape(self.clamped_c56*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c57*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c58*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c59*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c60*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c61*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c62*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c63*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg, tf.reshape(self.clamped_c64*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c1 *ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c2 *ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c3 *ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c4 *ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c5 *ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c6 *ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c7 *ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c8 *ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c9 *ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c10*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c11*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c12*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c13*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c14*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c15*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c16*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c17*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c18*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c19*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c20*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c21*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c22*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c23*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c24*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c25*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c26*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c27*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c28*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c29*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c30*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c31*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c32*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c33*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c34*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c35*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c36*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c37*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c38*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c39*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos, tf.reshape(self.clamped_c40*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c41*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c42*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c43*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c44*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c45*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c46*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c47*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg, tf.reshape(self.clamped_c48*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c49*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c50*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c51*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c52*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c53*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c54*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c55*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos, tf.reshape(self.clamped_c56*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c57*ws0_pos*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c58*ws0_pos*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c59*ws0_pos*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c60*ws0_pos*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c61*ws0_neg*ws1_pos*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c62*ws0_neg*ws1_pos*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c63*ws0_neg*ws1_neg*ws2_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg, tf.reshape(self.clamped_c64*ws0_neg*ws1_neg*ws2_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), [-1, self.nfilters]))



					#self.out=K.conv2d(x_pos[0,:,:,:,:]*xs_pos[0,:,:,:,:], kernel=self.clamped_w1, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_pos[0,:,:,:,:]*xs_neg[0,:,:,:,:], kernel=self.clamped_w2, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_neg[0,:,:,:,:]*xs_pos[0,:,:,:,:], kernel=self.clamped_w3, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_neg[0,:,:,:,:]*xs_neg[0,:,:,:,:], kernel=self.clamped_w4, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_pos[1,:,:,:,:]*xs_pos[1,:,:,:,:], kernel=self.clamped_w5, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_pos[1,:,:,:,:]*xs_neg[1,:,:,:,:], kernel=self.clamped_w6, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_neg[1,:,:,:,:]*xs_pos[1,:,:,:,:], kernel=self.clamped_w7, padding=self.padding,strides=self.strides )
					#self.out=self.out+K.conv2d(x_neg[1,:,:,:,:]*xs_neg[1,:,:,:,:], kernel=self.clamped_w8, padding=self.padding,strides=self.strides )

				else:
					x_expanded=0
					for l in range(self.levels):
						x_in=x[l,:,:,:,:]
						x_expanded=x_expanded+x_in
					self.out=K.conv2d(x_expanded, kernel=self.clamped_w*tf.tile(tf.reshape(self.pruning_mask, self.tile_size),[self.TRC,self.TRC,self.TM,self.TN]), padding=self.padding,strides=self.strides )
			elif self.levels==3:
				if self.LUT==True:
					x_pos=(1+x)/2
					x_neg=(1-x)/2
					self.out=K.conv2d(x_pos[0,:,:,:,:]*x_pos[1,:,:,:,:]*x_pos[2,:,:,:,:], kernel=self.clamped_w1, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_pos[0,:,:,:,:]*x_pos[1,:,:,:,:]*x_neg[2,:,:,:,:], kernel=self.clamped_w2, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_pos[0,:,:,:,:]*x_neg[1,:,:,:,:]*x_pos[2,:,:,:,:], kernel=self.clamped_w3, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_pos[0,:,:,:,:]*x_neg[1,:,:,:,:]*x_neg[2,:,:,:,:], kernel=self.clamped_w4, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_neg[0,:,:,:,:]*x_pos[1,:,:,:,:]*x_pos[2,:,:,:,:], kernel=self.clamped_w5, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_neg[0,:,:,:,:]*x_pos[1,:,:,:,:]*x_neg[2,:,:,:,:], kernel=self.clamped_w6, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_neg[0,:,:,:,:]*x_neg[1,:,:,:,:]*x_pos[2,:,:,:,:], kernel=self.clamped_w7, padding=self.padding,strides=self.strides )
					self.out=self.out+K.conv2d(x_neg[0,:,:,:,:]*x_neg[1,:,:,:,:]*x_neg[2,:,:,:,:], kernel=self.clamped_w8, padding=self.padding,strides=self.strides )
				else:
					x_expanded=0
					for l in range(self.levels):
						x_in=x[l,:,:,:,:]
						x_expanded=x_expanded+x_in
					self.out=K.conv2d(x_expanded, kernel=self.clamped_w, padding=self.padding,strides=self.strides )


		if keras.__version__[0]=='1':
			if self.levels==1:
                                self.out=K.conv2d(x, kernel=self.clamped_w, padding=self.padding,strides=self.strides )
                        else:
				for l in range(self.levels):
					x_expanded=x_expanded+x[l,:,:,:,:]
                                self.out=K.conv2d(x_expanded, kernel=self.clamped_w, padding=self.padding,strides=self.strides )

#		if keras.__version__[0]=='2':#train baseline with no resid gamma scaling
#			if self.levels==1:
#				self.out=K.conv2d(x, kernel=self.clamped_w, padding=self.padding,strides=self.strides )
#			elif self.levels==2:
#				x_expanded=0
#				for l in range(self.levels):
#					x_in=x[l,:,:,:,:]
#					x_expanded=x_expanded+x_in
#				self.out=K.conv2d(x_expanded, kernel=self.clamped_w, padding=self.padding,strides=self.strides )
#		if keras.__version__[0]=='1':
#			if self.levels==1:
#                                self.out=K.conv2d(x, kernel=self.clamped_w, padding=self.padding,strides=self.strides )
#                        else:
#				for l in range(self.levels):
#					x_expanded=x_expanded+x[l,:,:,:,:]
#                                self.out=K.conv2d(x_expanded, kernel=self.clamped_w, padding=self.padding,strides=self.strides )


		self.output_dim=self.out.get_shape()
		return self.out
	def  get_output_shape_for(self,input_shape):
		return (input_shape[0], self.output_dim[1],self.output_dim[2],self.output_dim[3])
	def compute_output_shape(self,input_shape):
		return (input_shape[0], self.output_dim[1],self.output_dim[2],self.output_dim[3])

class binary_dense(Layer):
	def __init__(self,n_in,n_out,levels=1,pruning_prob=0,first_layer=False,LUT=True,BINARY=True,TM=1,TN=1,**kwargs):
		self.n_in=n_in
		self.n_out=n_out
		self.levels=levels
		self.LUT=LUT
		self.BINARY=BINARY
		self.first_layer=first_layer
		self.TM = TM
		self.TN = TN
		self.tile_size = [n_in/TM, n_out/TN]
		super(binary_dense,self).__init__(**kwargs)
	def build(self, input_shape):
		self.rand_map_0 = self.add_weight(name='rand_map_0', 
			shape=(self.tile_size[0], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0], size=[self.tile_size[0], 1])),
			trainable=False) # Randomisation map for subsequent input connections
		self.rand_map_1 = self.add_weight(name='rand_map_1', 
			shape=(self.tile_size[0], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0], size=[self.tile_size[0], 1])),
			trainable=False) # Randomisation map for subsequent input connections

		self.rand_map_exp_0 = self.add_weight(name='rand_map_exp_0', 
			shape=(self.n_in, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.n_in, size=[self.n_in, 1])),
			trainable=False) # Randomisation map for subsequent input connections
		self.rand_map_exp_1 = self.add_weight(name='rand_map_exp_1', 
			shape=(self.n_in, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.n_in, size=[self.n_in, 1])),
			trainable=False) # Randomisation map for subsequent input connections

		stdv=1/np.sqrt(self.n_in)
		self.gamma=K.variable(1.0)
		if self.levels==1 or self.first_layer==True:
			w = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
			self.w=K.variable(w)
			self.trainable_weights=[self.w,self.gamma]
		elif self.levels==2:
			if self.LUT==True:
				w1  = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w2  = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w3  = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				c1  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c2  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c3  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c4  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c5  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c6  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c7  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c8  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c9  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c10 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c11 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c12 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c13 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c14 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c15 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c16 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c17 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c18 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c19 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c20 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c21 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c22 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c23 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c24 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c25 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c26 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c27 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c28 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c29 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c30 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c31 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c32 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c33 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c34 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c35 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c36 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c37 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c38 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c39 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c40 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c41 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c42 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c43 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c44 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c45 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c46 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c47 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c48 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c49 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c50 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c51 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c52 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c53 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c54 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c55 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c56 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c57 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c58 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c59 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c60 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c61 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c62 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c63 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c64 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)

#				self.w1 = self.add_weight(name='w1', 
#					shape=(self.n_in,self.n_out),
#					initializer=keras.initializers.Constant(value=np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)),
#					trainable=False) 
#				self.w2 = self.add_weight(name='w2', 
#					shape=(self.n_in,self.n_out),
#					initializer=keras.initializers.Constant(value=np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)),
#					trainable=False) 
#				self.w3 = self.add_weight(name='w3', 
#					shape=(self.n_in,self.n_out),
#					initializer=keras.initializers.Constant(value=np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)),
#					trainable=False) 

				self.c1 =K.variable(c1)
				self.c2 =K.variable(c2)
				self.c3 =K.variable(c3)
				self.c4 =K.variable(c4)
				self.c5 =K.variable(c5)
				self.c6 =K.variable(c6)
				self.c7 =K.variable(c7)
				self.c8 =K.variable(c8)
				self.c9 =K.variable(c9)
				self.c10=K.variable(c10)
				self.c11=K.variable(c11)
				self.c12=K.variable(c12)
				self.c13=K.variable(c13)
				self.c14=K.variable(c14)
				self.c15=K.variable(c15)
				self.c16=K.variable(c16)
				self.c17=K.variable(c17)
				self.c18=K.variable(c18)
				self.c19=K.variable(c19)
				self.c20=K.variable(c20)
				self.c21=K.variable(c21)
				self.c22=K.variable(c22)
				self.c23=K.variable(c23)
				self.c24=K.variable(c24)
				self.c25=K.variable(c25)
				self.c26=K.variable(c26)
				self.c27=K.variable(c27)
				self.c28=K.variable(c28)
				self.c29=K.variable(c29)
				self.c30=K.variable(c30)
				self.c31=K.variable(c31)
				self.c32=K.variable(c32)
				self.c33=K.variable(c33)
				self.c34=K.variable(c34)
				self.c35=K.variable(c35)
				self.c36=K.variable(c36)
				self.c37=K.variable(c37)
				self.c38=K.variable(c38)
				self.c39=K.variable(c39)
				self.c40=K.variable(c40)
				self.c41=K.variable(c41)
				self.c42=K.variable(c42)
				self.c43=K.variable(c43)
				self.c44=K.variable(c44)
				self.c45=K.variable(c45)
				self.c46=K.variable(c46)
				self.c47=K.variable(c47)
				self.c48=K.variable(c48)
				self.c49=K.variable(c49)
				self.c50=K.variable(c50)
				self.c51=K.variable(c51)
				self.c52=K.variable(c52)
				self.c53=K.variable(c53)
				self.c54=K.variable(c54)
				self.c55=K.variable(c55)
				self.c56=K.variable(c56)
				self.c57=K.variable(c57)
				self.c58=K.variable(c58)
				self.c59=K.variable(c59)
				self.c60=K.variable(c60)
				self.c61=K.variable(c61)
				self.c62=K.variable(c62)
				self.c63=K.variable(c63)
				self.c64=K.variable(c64)
				self.w1 =K.variable(w1)
				self.w2 =K.variable(w2)
				self.w3 =K.variable(w3)

				self.trainable_weights=[self.c1,self.c2,self.c3,self.c4,self.c5,self.c6,self.c7,self.c8,self.c9,self.c10,self.c11,self.c12,self.c13,self.c14,self.c15,self.c16,
					self.c17,self.c18,self.c19,self.c20,self.c21,self.c22,self.c23,self.c24,self.c25,self.c26,self.c27,self.c28,self.c29,self.c30,self.c31,self.c32,
					self.c33,self.c34,self.c35,self.c36,self.c37,self.c38,self.c39,self.c40,self.c41,self.c42,self.c43,self.c44,self.c45,self.c46,self.c47,self.c48,
					self.c49,self.c50,self.c51,self.c52,self.c53,self.c54,self.c55,self.c56,self.c57,self.c58,self.c59,self.c60,self.c61,self.c62,self.c63,self.c64,
					self.w1,self.w2,self.w3,self.gamma]



			else:
				w = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				self.w=K.variable(w)
				self.trainable_weights=[self.w,self.gamma]
		elif self.levels==3:
			if self.LUT==True:
				w1 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w2 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w3 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w4 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w5 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w6 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w7 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				w8 = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				self.w1=K.variable(w1)
				self.w2=K.variable(w2)
				self.w3=K.variable(w3)
				self.w4=K.variable(w4)
				self.w5=K.variable(w5)
				self.w6=K.variable(w6)
				self.w7=K.variable(w7)
				self.w8=K.variable(w8)

				self.trainable_weights=[self.w1,self.w2,self.w3,self.w4,self.w5,self.w6,self.w7,self.w8,self.gamma]
			else:
				w = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				self.w=K.variable(w)
				self.trainable_weights=[self.w,self.gamma]

		self.pruning_mask = self.add_weight(name='pruning_mask',
			shape=self.tile_size,
			initializer=keras.initializers.Constant(value=np.ones(self.tile_size)),
			trainable=False) # LUT pruning based on whether inputs get repeated


#		elif self.levels==2:#train baseline without resid gamma scaling
#			w = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
#			self.w=K.variable(w)
#			self.trainable_weights=[self.w,self.gamma]


	def call(self, x,mask=None):
		constraint_gamma=K.abs(self.gamma)#K.clip(self.gamma,0.01,10)
		if self.levels==1 or self.first_layer==True:
			if self.BINARY==False:
				self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
			else:
				self.clamped_w=constraint_gamma*binarize(self.w)
			self.out=K.dot(x,self.clamped_w)
		elif self.levels==2:
			if self.LUT==True:
				if self.BINARY==False:
					self.clamped_w1=K.clip(self.w1,-1,1)
					self.clamped_w2=K.clip(self.w2,-1,1)
					self.clamped_w3=K.clip(self.w3,-1,1)
	
					self.clamped_c1= constraint_gamma*K.clip(tf.tile(self.c1, [self.TM,self.TN]),-1,1)
					self.clamped_c2= constraint_gamma*K.clip(tf.tile(self.c2, [self.TM,self.TN]),-1,1)
					self.clamped_c3= constraint_gamma*K.clip(tf.tile(self.c3, [self.TM,self.TN]),-1,1)
					self.clamped_c4= constraint_gamma*K.clip(tf.tile(self.c4, [self.TM,self.TN]),-1,1)
					self.clamped_c5= constraint_gamma*K.clip(tf.tile(self.c5, [self.TM,self.TN]),-1,1)
					self.clamped_c6= constraint_gamma*K.clip(tf.tile(self.c6, [self.TM,self.TN]),-1,1)
					self.clamped_c7= constraint_gamma*K.clip(tf.tile(self.c7, [self.TM,self.TN]),-1,1)
					self.clamped_c8= constraint_gamma*K.clip(tf.tile(self.c8, [self.TM,self.TN]),-1,1)
					self.clamped_c9= constraint_gamma*K.clip(tf.tile(self.c9, [self.TM,self.TN]),-1,1)
					self.clamped_c10=constraint_gamma*K.clip(tf.tile(self.c10,[self.TM,self.TN]),-1,1)
					self.clamped_c11=constraint_gamma*K.clip(tf.tile(self.c11,[self.TM,self.TN]),-1,1)
					self.clamped_c12=constraint_gamma*K.clip(tf.tile(self.c12,[self.TM,self.TN]),-1,1)
					self.clamped_c13=constraint_gamma*K.clip(tf.tile(self.c13,[self.TM,self.TN]),-1,1)
					self.clamped_c14=constraint_gamma*K.clip(tf.tile(self.c14,[self.TM,self.TN]),-1,1)
					self.clamped_c15=constraint_gamma*K.clip(tf.tile(self.c15,[self.TM,self.TN]),-1,1)
					self.clamped_c16=constraint_gamma*K.clip(tf.tile(self.c16,[self.TM,self.TN]),-1,1)
					self.clamped_c17=constraint_gamma*K.clip(tf.tile(self.c17,[self.TM,self.TN]),-1,1)
					self.clamped_c18=constraint_gamma*K.clip(tf.tile(self.c18,[self.TM,self.TN]),-1,1)
					self.clamped_c19=constraint_gamma*K.clip(tf.tile(self.c19,[self.TM,self.TN]),-1,1)
					self.clamped_c20=constraint_gamma*K.clip(tf.tile(self.c20,[self.TM,self.TN]),-1,1)
					self.clamped_c21=constraint_gamma*K.clip(tf.tile(self.c21,[self.TM,self.TN]),-1,1)
					self.clamped_c22=constraint_gamma*K.clip(tf.tile(self.c22,[self.TM,self.TN]),-1,1)
					self.clamped_c23=constraint_gamma*K.clip(tf.tile(self.c23,[self.TM,self.TN]),-1,1)
					self.clamped_c24=constraint_gamma*K.clip(tf.tile(self.c24,[self.TM,self.TN]),-1,1)
					self.clamped_c25=constraint_gamma*K.clip(tf.tile(self.c25,[self.TM,self.TN]),-1,1)
					self.clamped_c26=constraint_gamma*K.clip(tf.tile(self.c26,[self.TM,self.TN]),-1,1)
					self.clamped_c27=constraint_gamma*K.clip(tf.tile(self.c27,[self.TM,self.TN]),-1,1)
					self.clamped_c28=constraint_gamma*K.clip(tf.tile(self.c28,[self.TM,self.TN]),-1,1)
					self.clamped_c29=constraint_gamma*K.clip(tf.tile(self.c29,[self.TM,self.TN]),-1,1)
					self.clamped_c30=constraint_gamma*K.clip(tf.tile(self.c30,[self.TM,self.TN]),-1,1)
					self.clamped_c31=constraint_gamma*K.clip(tf.tile(self.c31,[self.TM,self.TN]),-1,1)
					self.clamped_c32=constraint_gamma*K.clip(tf.tile(self.c32,[self.TM,self.TN]),-1,1)
					self.clamped_c33=constraint_gamma*K.clip(tf.tile(self.c33,[self.TM,self.TN]),-1,1)
					self.clamped_c34=constraint_gamma*K.clip(tf.tile(self.c34,[self.TM,self.TN]),-1,1)
					self.clamped_c35=constraint_gamma*K.clip(tf.tile(self.c35,[self.TM,self.TN]),-1,1)
					self.clamped_c36=constraint_gamma*K.clip(tf.tile(self.c36,[self.TM,self.TN]),-1,1)
					self.clamped_c37=constraint_gamma*K.clip(tf.tile(self.c37,[self.TM,self.TN]),-1,1)
					self.clamped_c38=constraint_gamma*K.clip(tf.tile(self.c38,[self.TM,self.TN]),-1,1)
					self.clamped_c39=constraint_gamma*K.clip(tf.tile(self.c39,[self.TM,self.TN]),-1,1)
					self.clamped_c40=constraint_gamma*K.clip(tf.tile(self.c40,[self.TM,self.TN]),-1,1)
					self.clamped_c41=constraint_gamma*K.clip(tf.tile(self.c41,[self.TM,self.TN]),-1,1)
					self.clamped_c42=constraint_gamma*K.clip(tf.tile(self.c42,[self.TM,self.TN]),-1,1)
					self.clamped_c43=constraint_gamma*K.clip(tf.tile(self.c43,[self.TM,self.TN]),-1,1)
					self.clamped_c44=constraint_gamma*K.clip(tf.tile(self.c44,[self.TM,self.TN]),-1,1)
					self.clamped_c45=constraint_gamma*K.clip(tf.tile(self.c45,[self.TM,self.TN]),-1,1)
					self.clamped_c46=constraint_gamma*K.clip(tf.tile(self.c46,[self.TM,self.TN]),-1,1)
					self.clamped_c47=constraint_gamma*K.clip(tf.tile(self.c47,[self.TM,self.TN]),-1,1)
					self.clamped_c48=constraint_gamma*K.clip(tf.tile(self.c48,[self.TM,self.TN]),-1,1)
					self.clamped_c49=constraint_gamma*K.clip(tf.tile(self.c49,[self.TM,self.TN]),-1,1)
					self.clamped_c50=constraint_gamma*K.clip(tf.tile(self.c50,[self.TM,self.TN]),-1,1)
					self.clamped_c51=constraint_gamma*K.clip(tf.tile(self.c51,[self.TM,self.TN]),-1,1)
					self.clamped_c52=constraint_gamma*K.clip(tf.tile(self.c52,[self.TM,self.TN]),-1,1)
					self.clamped_c53=constraint_gamma*K.clip(tf.tile(self.c53,[self.TM,self.TN]),-1,1)
					self.clamped_c54=constraint_gamma*K.clip(tf.tile(self.c54,[self.TM,self.TN]),-1,1)
					self.clamped_c55=constraint_gamma*K.clip(tf.tile(self.c55,[self.TM,self.TN]),-1,1)
					self.clamped_c56=constraint_gamma*K.clip(tf.tile(self.c56,[self.TM,self.TN]),-1,1)
					self.clamped_c57=constraint_gamma*K.clip(tf.tile(self.c57,[self.TM,self.TN]),-1,1)
					self.clamped_c58=constraint_gamma*K.clip(tf.tile(self.c58,[self.TM,self.TN]),-1,1)
					self.clamped_c59=constraint_gamma*K.clip(tf.tile(self.c59,[self.TM,self.TN]),-1,1)
					self.clamped_c60=constraint_gamma*K.clip(tf.tile(self.c60,[self.TM,self.TN]),-1,1)
					self.clamped_c61=constraint_gamma*K.clip(tf.tile(self.c61,[self.TM,self.TN]),-1,1)
					self.clamped_c62=constraint_gamma*K.clip(tf.tile(self.c62,[self.TM,self.TN]),-1,1)
					self.clamped_c63=constraint_gamma*K.clip(tf.tile(self.c63,[self.TM,self.TN]),-1,1)
					self.clamped_c64=constraint_gamma*K.clip(tf.tile(self.c64,[self.TM,self.TN]),-1,1)

				else:
					self.clamped_w1 =binarize(self.w1)
					self.clamped_w2 =binarize(self.w2)
					self.clamped_w3 =binarize(self.w3)
	
					self.clamped_c1= constraint_gamma*binarize(tf.tile(self.c1, [self.TM,self.TN]))
					self.clamped_c2= constraint_gamma*binarize(tf.tile(self.c2, [self.TM,self.TN]))
					self.clamped_c3= constraint_gamma*binarize(tf.tile(self.c3, [self.TM,self.TN]))
					self.clamped_c4= constraint_gamma*binarize(tf.tile(self.c4, [self.TM,self.TN]))
					self.clamped_c5= constraint_gamma*binarize(tf.tile(self.c5, [self.TM,self.TN]))
					self.clamped_c6= constraint_gamma*binarize(tf.tile(self.c6, [self.TM,self.TN]))
					self.clamped_c7= constraint_gamma*binarize(tf.tile(self.c7, [self.TM,self.TN]))
					self.clamped_c8= constraint_gamma*binarize(tf.tile(self.c8, [self.TM,self.TN]))
					self.clamped_c9= constraint_gamma*binarize(tf.tile(self.c9, [self.TM,self.TN]))
					self.clamped_c10=constraint_gamma*binarize(tf.tile(self.c10,[self.TM,self.TN]))
					self.clamped_c11=constraint_gamma*binarize(tf.tile(self.c11,[self.TM,self.TN]))
					self.clamped_c12=constraint_gamma*binarize(tf.tile(self.c12,[self.TM,self.TN]))
					self.clamped_c13=constraint_gamma*binarize(tf.tile(self.c13,[self.TM,self.TN]))
					self.clamped_c14=constraint_gamma*binarize(tf.tile(self.c14,[self.TM,self.TN]))
					self.clamped_c15=constraint_gamma*binarize(tf.tile(self.c15,[self.TM,self.TN]))
					self.clamped_c16=constraint_gamma*binarize(tf.tile(self.c16,[self.TM,self.TN]))
					self.clamped_c17=constraint_gamma*binarize(tf.tile(self.c17,[self.TM,self.TN]))
					self.clamped_c18=constraint_gamma*binarize(tf.tile(self.c18,[self.TM,self.TN]))
					self.clamped_c19=constraint_gamma*binarize(tf.tile(self.c19,[self.TM,self.TN]))
					self.clamped_c20=constraint_gamma*binarize(tf.tile(self.c20,[self.TM,self.TN]))
					self.clamped_c21=constraint_gamma*binarize(tf.tile(self.c21,[self.TM,self.TN]))
					self.clamped_c22=constraint_gamma*binarize(tf.tile(self.c22,[self.TM,self.TN]))
					self.clamped_c23=constraint_gamma*binarize(tf.tile(self.c23,[self.TM,self.TN]))
					self.clamped_c24=constraint_gamma*binarize(tf.tile(self.c24,[self.TM,self.TN]))
					self.clamped_c25=constraint_gamma*binarize(tf.tile(self.c25,[self.TM,self.TN]))
					self.clamped_c26=constraint_gamma*binarize(tf.tile(self.c26,[self.TM,self.TN]))
					self.clamped_c27=constraint_gamma*binarize(tf.tile(self.c27,[self.TM,self.TN]))
					self.clamped_c28=constraint_gamma*binarize(tf.tile(self.c28,[self.TM,self.TN]))
					self.clamped_c29=constraint_gamma*binarize(tf.tile(self.c29,[self.TM,self.TN]))
					self.clamped_c30=constraint_gamma*binarize(tf.tile(self.c30,[self.TM,self.TN]))
					self.clamped_c31=constraint_gamma*binarize(tf.tile(self.c31,[self.TM,self.TN]))
					self.clamped_c32=constraint_gamma*binarize(tf.tile(self.c32,[self.TM,self.TN]))
					self.clamped_c33=constraint_gamma*binarize(tf.tile(self.c33,[self.TM,self.TN]))
					self.clamped_c34=constraint_gamma*binarize(tf.tile(self.c34,[self.TM,self.TN]))
					self.clamped_c35=constraint_gamma*binarize(tf.tile(self.c35,[self.TM,self.TN]))
					self.clamped_c36=constraint_gamma*binarize(tf.tile(self.c36,[self.TM,self.TN]))
					self.clamped_c37=constraint_gamma*binarize(tf.tile(self.c37,[self.TM,self.TN]))
					self.clamped_c38=constraint_gamma*binarize(tf.tile(self.c38,[self.TM,self.TN]))
					self.clamped_c39=constraint_gamma*binarize(tf.tile(self.c39,[self.TM,self.TN]))
					self.clamped_c40=constraint_gamma*binarize(tf.tile(self.c40,[self.TM,self.TN]))
					self.clamped_c41=constraint_gamma*binarize(tf.tile(self.c41,[self.TM,self.TN]))
					self.clamped_c42=constraint_gamma*binarize(tf.tile(self.c42,[self.TM,self.TN]))
					self.clamped_c43=constraint_gamma*binarize(tf.tile(self.c43,[self.TM,self.TN]))
					self.clamped_c44=constraint_gamma*binarize(tf.tile(self.c44,[self.TM,self.TN]))
					self.clamped_c45=constraint_gamma*binarize(tf.tile(self.c45,[self.TM,self.TN]))
					self.clamped_c46=constraint_gamma*binarize(tf.tile(self.c46,[self.TM,self.TN]))
					self.clamped_c47=constraint_gamma*binarize(tf.tile(self.c47,[self.TM,self.TN]))
					self.clamped_c48=constraint_gamma*binarize(tf.tile(self.c48,[self.TM,self.TN]))
					self.clamped_c49=constraint_gamma*binarize(tf.tile(self.c49,[self.TM,self.TN]))
					self.clamped_c50=constraint_gamma*binarize(tf.tile(self.c50,[self.TM,self.TN]))
					self.clamped_c51=constraint_gamma*binarize(tf.tile(self.c51,[self.TM,self.TN]))
					self.clamped_c52=constraint_gamma*binarize(tf.tile(self.c52,[self.TM,self.TN]))
					self.clamped_c53=constraint_gamma*binarize(tf.tile(self.c53,[self.TM,self.TN]))
					self.clamped_c54=constraint_gamma*binarize(tf.tile(self.c54,[self.TM,self.TN]))
					self.clamped_c55=constraint_gamma*binarize(tf.tile(self.c55,[self.TM,self.TN]))
					self.clamped_c56=constraint_gamma*binarize(tf.tile(self.c56,[self.TM,self.TN]))
					self.clamped_c57=constraint_gamma*binarize(tf.tile(self.c57,[self.TM,self.TN]))
					self.clamped_c58=constraint_gamma*binarize(tf.tile(self.c58,[self.TM,self.TN]))
					self.clamped_c59=constraint_gamma*binarize(tf.tile(self.c59,[self.TM,self.TN]))
					self.clamped_c60=constraint_gamma*binarize(tf.tile(self.c60,[self.TM,self.TN]))
					self.clamped_c61=constraint_gamma*binarize(tf.tile(self.c61,[self.TM,self.TN]))
					self.clamped_c62=constraint_gamma*binarize(tf.tile(self.c62,[self.TM,self.TN]))
					self.clamped_c63=constraint_gamma*binarize(tf.tile(self.c63,[self.TM,self.TN]))
					self.clamped_c64=constraint_gamma*binarize(tf.tile(self.c64,[self.TM,self.TN]))

				# Special hack for randomising the subsequent input connections: tensorflow does not support advanced matrix indexing
				shuf_x=tf.transpose(x, perm=[2, 0, 1])
				shuf_x_0 = tf.gather_nd(shuf_x, tf.cast(self.rand_map_exp_0, tf.int32))
				shuf_x_0=tf.transpose(shuf_x_0, perm=[1, 2, 0])

				shuf_x_1 = tf.gather_nd(shuf_x, tf.cast(self.rand_map_exp_1, tf.int32))
				shuf_x_1=tf.transpose(shuf_x_1, perm=[1, 2, 0])

				x_pos=(1+binarize(x))/2*abs(x)
				x_neg=(1-binarize(x))/2*abs(x)
				xs0_pos=(1+binarize(shuf_x_0))/2#*abs(shuf_x_0)
				xs0_neg=(1-binarize(shuf_x_0))/2#*abs(shuf_x_0)
				xs1_pos=(1+binarize(shuf_x_1))/2#*abs(shuf_x_0)
				xs1_neg=(1-binarize(shuf_x_1))/2#*abs(shuf_x_0)

				ws0_pos=(1+binarize(self.clamped_w1))/2
				ws0_neg=(1-binarize(self.clamped_w1))/2
				ws1_pos=(1+binarize(self.clamped_w2))/2
				ws1_neg=(1-binarize(self.clamped_w2))/2
				ws2_pos=(1+binarize(self.clamped_w3))/2
				ws2_neg=(1-binarize(self.clamped_w3))/2

				self.out=         K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c1 *ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c2 *ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c3 *ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c4 *ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c5 *ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c6 *ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c7 *ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c8 *ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c9 *ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c10*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c11*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c12*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c13*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c14*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c15*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c16*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c17*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c18*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c19*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c20*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c21*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c22*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c23*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c24*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c25*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c26*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c27*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c28*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c29*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c30*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c31*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c32*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c33*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c34*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c35*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c36*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c37*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c38*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c39*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:],self.clamped_c40*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c41*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c42*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c43*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c44*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c45*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c46*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c47*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:],self.clamped_c48*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c49*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c50*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c51*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c52*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c53*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c54*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c55*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:],self.clamped_c56*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c57*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c58*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c59*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c60*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c61*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c62*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c63*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:],self.clamped_c64*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c1 *ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c2 *ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c3 *ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c4 *ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c5 *ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c6 *ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c7 *ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c8 *ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c9 *ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c10*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c11*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c12*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c13*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c14*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c15*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c16*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c17*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c18*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c19*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c20*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c21*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c22*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c23*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c24*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c25*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c26*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c27*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c28*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c29*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c30*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c31*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c32*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c33*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c34*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c35*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c36*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c37*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c38*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c39*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:],self.clamped_c40*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c41*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c42*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c43*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c44*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c45*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c46*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c47*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:],self.clamped_c48*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c49*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c50*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c51*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c52*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c53*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c54*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c55*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:],self.clamped_c56*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c57*ws0_pos*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c58*ws0_pos*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c59*ws0_pos*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c60*ws0_pos*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c61*ws0_neg*ws1_pos*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c62*ws0_neg*ws1_pos*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c63*ws0_neg*ws1_neg*ws2_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:],self.clamped_c64*ws0_neg*ws1_neg*ws2_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))

			else:
				x_expanded=0
				if self.BINARY==False:
					self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
				else:
					self.clamped_w=constraint_gamma*binarize(self.w)
				for l in range(self.levels):
					x_expanded=x_expanded+x[l,:,:]
				self.out=K.dot(x_expanded,self.clamped_w*tf.tile(self.pruning_mask,[self.TM,self.TN]))
		elif self.levels==3:
			if self.LUT==True:
				self.clamped_w1=constraint_gamma*binarize(self.w1)
				self.clamped_w2=constraint_gamma*binarize(self.w2)
				self.clamped_w3=constraint_gamma*binarize(self.w3)
				self.clamped_w4=constraint_gamma*binarize(self.w4)
				self.clamped_w5=constraint_gamma*binarize(self.w5)
				self.clamped_w6=constraint_gamma*binarize(self.w6)
				self.clamped_w7=constraint_gamma*binarize(self.w7)
				self.clamped_w8=constraint_gamma*binarize(self.w8)
				x_pos=(1+x)/2
				x_neg=(1-x)/2
				self.out=K.dot(x_pos[0,:,:]*x_pos[1,:,:]*x_pos[2,:,:],self.clamped_w1)
				self.out=self.out+K.dot(x_pos[0,:,:]*x_pos[1,:,:]*x_neg[2,:,:],self.clamped_w2)
				self.out=self.out+K.dot(x_pos[0,:,:]*x_neg[1,:,:]*x_pos[2,:,:],self.clamped_w3)
				self.out=self.out+K.dot(x_pos[0,:,:]*x_neg[1,:,:]*x_neg[2,:,:],self.clamped_w4)
				self.out=self.out+K.dot(x_neg[0,:,:]*x_pos[1,:,:]*x_pos[2,:,:],self.clamped_w5)
				self.out=self.out+K.dot(x_neg[0,:,:]*x_pos[1,:,:]*x_neg[2,:,:],self.clamped_w6)
				self.out=self.out+K.dot(x_neg[0,:,:]*x_neg[1,:,:]*x_pos[2,:,:],self.clamped_w7)
				self.out=self.out+K.dot(x_neg[0,:,:]*x_neg[1,:,:]*x_neg[2,:,:],self.clamped_w8)
			else:
				x_expanded=0
				self.clamped_w=constraint_gamma*binarize(self.w)
				for l in range(self.levels):
					x_expanded=x_expanded+x[l,:,:]
				self.out=K.dot(x_expanded,self.clamped_w)


#		x_expanded=0
#		if self.levels==1:
#			self.clamped_w=constraint_gamma*binarize(self.w)
#			self.out=K.dot(x,self.clamped_w)
#		else:
#			self.clamped_w=constraint_gamma*binarize(self.w)
#			for l in range(self.levels):
#				x_expanded=x_expanded+x[l,:,:]
#			self.out=K.dot(x_expanded,self.clamped_w)
		return self.out
	def  get_output_shape_for(self,input_shape):
		return (input_shape[0], self.n_out)
	def compute_output_shape(self,input_shape):
		return (input_shape[0], self.n_out)



"""
def binarize(x):
    #Clip and binarize tensor using the straight through estimator (STE) for the gradient.
    g = tf.get_default_graph()
    with ops.name_scope("Binarized") as name:
        with g.gradient_override_map({"Sign": "Identity"}):
            x=tf.clip_by_value(x,-1,1)
            return tf.sign(x)

class Residual_sign(Layer):
	def __init__(self, levels=1,**kwargs):
		self.levels=levels
		super(Residual_sign, self).__init__(**kwargs)
	def build(self, input_shape):
		ars=np.arange(self.levels)+1.0
		ars=ars[::-1]
		self.means=ars/np.sum(ars)
		self.means=tf.Variable(self.means,dtype=tf.float32)
		K.get_session().run(tf.variables_initializer([self.means]))
		self.trainable_weights=[self.means]

	def call(self, x,mask=None):
		resid = x
		out_bin=0
		for l in range(self.levels):
			out=binarize(resid)*K.abs(self.means[l])
			out_bin=out_bin+out
			resid=resid-out
		return out_bin

	def compute_output_shape(self,input_shape):
		return input_shape
	def set_means(self,X):
		means=np.zeros((self.levels))
		means[0]=1
		resid=np.clip(X,-1,1)
		approx=0
		for l in range(self.levels):
			m=np.mean(np.absolute(resid))
			out=np.sign(resid)*m
			approx=approx+out
			resid=resid-out
			means[l]=m
			err=np.mean((approx-np.clip(X,-1,1))**2)

		means=means/np.sum(means)
		sess=K.get_session()
		sess.run(self.means.assign(means))

class binary_conv(Layer):
	def __init__(self,nfilters,ch_in,k,padding,**kwargs):
		self.nfilters=nfilters
		self.ch_in=ch_in
		self.k=k
		self.padding=padding
		super(binary_conv,self).__init__(**kwargs)
	def build(self, input_shape):
		stdv=1/np.sqrt(self.k*self.k*self.ch_in)
		w = tf.random_normal(shape=[self.k,self.k,self.ch_in,self.nfilters], mean=0.0, stddev=stdv, dtype=tf.float32)
		self.w=K.variable(w)
		self.gamma=K.variable([1.0])
		self.trainable_weights=[self.w,self.gamma]
	def call(self, x,mask=None):
		constraint_gamma=K.abs(self.gamma)
		self.clamped_w=constraint_gamma*binarize(self.w)
		self.out=K.conv2d(x, kernel=self.clamped_w, padding=self.padding)#tf.nn.convolution(x, filter=self.clamped_w , padding=self.padding)
		self.output_dim=self.out.get_shape()
		#self.out=Convolution2D(filters=32, kernel_size=(3,3), strides=(1, 1), padding='valid', use_bias=False)(x)
		return self.out
	def  compute_output_shape(self,input_shape):
		return (input_shape[0], self.output_dim[1],self.output_dim[2],self.output_dim[3])

class binary_dense(Layer):
	def __init__(self,n_in,n_out,**kwargs):
		self.n_in=n_in
		self.n_out=n_out
		super(binary_dense,self).__init__(**kwargs)
	def build(self, input_shape):
		stdv=1/np.sqrt(self.n_in)
		w = tf.random_normal(shape=[self.n_in,self.n_out], mean=0.0, stddev=stdv, dtype=tf.float32)
		self.w=K.variable(w)
		self.gamma=K.variable([1.0])
		self.trainable_weights=[self.w,self.gamma]
	def call(self, x, mask=None):
		constraint_gamma=K.abs(self.gamma)
		self.clamped_w=constraint_gamma*binarize(self.w)
		self.out=K.dot(x, self.clamped_w)
		self.output_dim=self.out.get_shape()
		return self.out
	def  compute_output_shape(self,input_shape):
		return (input_shape[0], self.output_dim[1])
"""
class my_flat(Layer):
	def __init__(self,**kwargs):
		super(my_flat,self).__init__(**kwargs)
	def build(self, input_shape):
		return

	def call(self, x, mask=None):
		self.out=tf.reshape(x,[-1,np.prod(x.get_shape().as_list()[1:])])
		return self.out
	def  compute_output_shape(self,input_shape):
		shpe=(input_shape[0],int(np.prod(input_shape[1:])))
		return shpe
